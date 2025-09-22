print("--- CLI Module Loaded ---")
import typer
import asyncio
import uvloop
from typing import Optional

from src.app import ArbiBotApp
from src.core.config import load_config, TokenDetails
from src.core.types import MarketPair
from backtest.datasets import load_dataset
from backtest.simulator import Simulator
from src.strategy.rebalancer import Rebalancer
from src.exchange.binance import BinanceCexClient
from src.scanner import VolumeScannerService, VolumeSpikeScanner, SpikeArbitrageEvaluator

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
def rebalance(paper_run: bool = typer.Option(False, "--paper-run", help="執行紙上交易模式，不下單")):
    """
    執行一次性的資產再平衡檢查與操作。
    """
    if paper_run:
        typer.echo("正在執行資產再平衡 (Paper Run 模式)...")
    else:
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
            dex_pool_fee=p.dex_pool_fee,
            base_precision=p.base_precision if p.base_precision is not None else 8,
            quote_precision=p.quote_precision if p.quote_precision is not None else 8,
        ) for p in config.pairs]
        cex_client = BinanceCexClient(config.cex, config.secrets, pairs)
        await cex_client.connect()
        
        rebalancer = Rebalancer(config, cex_client)
        await rebalancer.run_rebalance_check(paper_run=paper_run)
        
        await cex_client.close()

    try:
        asyncio.run(main())
    except Exception as e:
        typer.echo(f"執行再平衡時發生錯誤: {e}", err=True)
    finally:
        typer.echo("再平衡任務結束。")


@app.command()
def check_dex_balance():
    """
    檢查並顯示 DEX 錢包中所有已設定代幣的餘額。
    """
    typer.echo("正在查詢 DEX 錢包餘額...")
    uvloop.install()
    config = load_config()

    # Dynamically import here to avoid circular dependency if main grows
    from src.exchange.univ3 import UniV3DexClient

    async def main():
        dex_client = UniV3DexClient(config.dex, config.network, config.secrets, config.tokens)
        
        typer.echo(f"錢包地址: {dex_client.user_address}")
        typer.echo("-" * 40)

        # 遍歷所有設定的代幣和鏈
        for token_symbol, chains in config.tokens.items():
            for chain in chains.keys():
                typer.echo(f"正在查詢 {token_symbol} on {chain}...")
                balance = await dex_client.get_balance(token_symbol, chain)
                if balance >= 0:
                    typer.secho(f"  -> {balance:.8f} {token_symbol}", fg=typer.colors.GREEN)
                else:
                    typer.secho(f"  -> 查詢失敗", fg=typer.colors.RED)
        
        # 額外檢查原生代幣
        for chain, symbol in config.network.native_token.items():
             if symbol not in config.tokens:
                typer.echo(f"正在查詢原生代幣 {symbol} on {chain}...")
                balance = await dex_client.get_balance(symbol, chain)
                if balance >= 0:
                    typer.secho(f"  -> {balance:.8f} {symbol}", fg=typer.colors.GREEN)
                else:
                    typer.secho(f"  -> 查詢失敗", fg=typer.colors.RED)


    try:
        asyncio.run(main())
    except Exception as e:
        typer.echo(f"查詢餘額時發生錯誤: {e}", err=True)
    finally:
        typer.echo("DEX 餘額查詢完畢。")


@app.command(name="discover_pairs")
def discover_pairs_command():
    """透過 1h 交易量異常掃描新增可監控的交易對。"""
    typer.echo("正在執行 Volume Scanner 以探索新的交易對...")
    uvloop.install()
    config = load_config()
    scanner = VolumeScannerService(config)

    async def main():
        return await scanner.run()

    try:
        results = asyncio.run(main())
    except Exception as exc:
        typer.secho(f"Volume Scanner 執行失敗: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    if not results:
        typer.echo("未發現新的候選交易對。")
        return

    typer.echo("已發現以下可監控交易對：")
    for item in results:
        pair = item.pair_config
        typer.secho(
            f" - {pair.cex_symbol} on {pair.dex_chain} (fee {pair.dex_pool_fee})",
            fg=typer.colors.GREEN,
        )
    typer.echo(f"詳細組態已更新至 {config.scanner.volume.persist_path}")


# 破折號別名，便於與舊文件相容
@app.command(name="discover-pairs")
def discover_pairs_alias():
    return discover_pairs_command()


@app.command()
def lookup_pool(
    base: str = typer.Argument(..., help="基礎貨幣符號，例如 REZ"),
    quote: str = typer.Argument(..., help="報價貨幣符號，例如 USDC"),
    chain: str = typer.Option("ethereum", "--chain", help="Uniswap 所在鏈，例如 ethereum"),
    fee: int = typer.Option(3000, "--fee", help="池子的手續費等級，例如 500, 3000, 10000"),
    base_address: Optional[str] = typer.Option(None, "--base-address", help="若 tokens.yaml 未設定，可直接提供基礎貨幣地址"),
    base_decimals: int = typer.Option(18, "--base-decimals", help="基礎貨幣小數位（自訂地址時使用）"),
    quote_address: Optional[str] = typer.Option(None, "--quote-address", help="若 tokens.yaml 未設定，可直接提供報價貨幣地址"),
    quote_decimals: int = typer.Option(6, "--quote-decimals", help="報價貨幣小數位（自訂地址時使用）"),
):
    """手動查詢 Uniswap V3 pool 是否存在。"""
    typer.echo(f"正在查詢 {base}/{quote} on {chain} fee {fee}...")
    uvloop.install()

    from src.exchange.univ3 import UniV3DexClient  # 避免循環引用

    config = load_config()

    async def main():
        client = UniV3DexClient(config.dex, config.network, config.secrets, config.tokens)
        if base_address:
            client.tokens_config.setdefault(base, {})[chain] = TokenDetails(address=base_address, decimals=base_decimals)
        if quote_address:
            client.tokens_config.setdefault(quote, {})[chain] = TokenDetails(address=quote_address, decimals=quote_decimals)
        return await client.get_pool_address(base, quote, chain, fee)

    try:
        pool = asyncio.run(main())
    except Exception as exc:
        typer.secho(f"查詢過程失敗: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    if pool:
        typer.secho(f"找到池子位址: {pool}", fg=typer.colors.GREEN)
    else:
        typer.echo("未發現對應的池子。")


@app.command()
def spike_run(
    evaluate: bool = typer.Option(True, "--evaluate/--no-evaluate", help="掃描後是否立即評估套利訊號"),
    use_existing: bool = typer.Option(False, "--use-existing", help="僅使用現有 JSON，不重新掃描"),
):
    """執行 Binance Volume spike 掃描並視需要進行套利評估。"""
    uvloop.install()
    config = load_config()
    scanner = VolumeSpikeScanner(config)
    evaluator = SpikeArbitrageEvaluator(config)

    async def main():
        spikes = scanner.load_spikes() if use_existing else await scanner.scan()
        signals = await evaluator.evaluate(spikes) if evaluate else []
        return spikes, signals

    try:
        spikes, signals = asyncio.run(main())
    except Exception as exc:
        typer.secho(f"spike_run 執行失敗: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Volume spike 候選數量: {len(spikes)}")
    if evaluate and signals:
        typer.secho("檢測到以下套利訊號:", fg=typer.colors.GREEN)
        for signal in signals:
            data = signal.as_dict()
            typer.echo(
                f" - {data['symbol']} | {data['direction']} | edge={data['effective_edge_bps']} bps | "
                f"CEX={data['cex_price']:.6g} | DEX={data['dex_price']:.6g} on {data['dex_chain']} (fee {data['dex_fee_tier']})"
            )
    elif evaluate:
        typer.echo("目前沒有符合閾值的套利訊號。")


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
