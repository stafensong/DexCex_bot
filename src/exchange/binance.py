import asyncio
import orjson
import time
import hmac
import hashlib
from decimal import Decimal
from typing import Optional, Dict, List, TYPE_CHECKING

import aiohttp
import websockets
from loguru import logger

from ..core.config import CexConfig, SecretsConfig
from ..core.types import MarketPair, Quote
from .cex_base import CexClient, CexOrder, OrderUpdate
from ..infra import metrics

if TYPE_CHECKING:  # pragma: no cover
    from ..infra.dashboard import DashboardPublisher

USER_STREAM_KEEPALIVE_SECONDS = 25 * 60  # 少於 30 分鐘的官方要求


class BinanceCexClient(CexClient):
    def __init__(
        self,
        config: CexConfig,
        secrets: SecretsConfig,
        pairs: List[MarketPair],
        dashboard_publisher: Optional["DashboardPublisher"] = None,
    ):
        self.config = config
        self.secrets = secrets
        self.pairs = pairs
        self.base_url = config.base_url.rstrip('/')
        self.ws_url = config.ws_url.rstrip('/')
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws_conn: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_listener_task: Optional[asyncio.Task] = None
        self._user_stream_task: Optional[asyncio.Task] = None
        self._listen_key_keepalive_task: Optional[asyncio.Task] = None
        self.orderbooks: Dict[str, Dict[str, Decimal]] = {p.cex_symbol.replace('/', ''): {'bids': {}, 'asks': {}} for p in self.pairs}
        self.last_update_ids: Dict[str, int] = {}
        self._listen_key: Optional[str] = None
        self._closing = False
        self._order_pair_cache: Dict[str, MarketPair] = {}
        self.dashboard_publisher = dashboard_publisher

    async def connect(self):
        logger.info("正在連接到 Binance...")
        self._closing = False
        self._session = aiohttp.ClientSession(
            headers={
                'X-MBX-APIKEY': self.secrets.binance_api_key.get_secret_value()
            }
        )
        # 啟動 WebSocket 監聽任務
        self._ws_listener_task = asyncio.create_task(self._ws_listener())
        await self._start_user_stream()
        logger.info("Binance 客戶端已連接並啟動 WebSocket 監聽。")

    async def close(self):
        self._closing = True
        if self._ws_listener_task:
            self._ws_listener_task.cancel()
        if self._user_stream_task:
            self._user_stream_task.cancel()
        if self._listen_key_keepalive_task:
            self._listen_key_keepalive_task.cancel()
        if self._listen_key:
            await self._delete_listen_key()
        if self._ws_conn:
            await self._ws_conn.close()
        if self._session:
            await self._session.close()
        logger.info("Binance 連線已關閉。")

    def _get_signature(self, params: dict) -> str:
        query_string = '&'.join([f"{k}={v}" for k, v in params.items()])
        return hmac.new(self.secrets.binance_api_secret.get_secret_value().encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()

    async def _start_user_stream(self) -> None:
        if not self._session:
            logger.warning("User stream 初始化失敗：Session 尚未建立。")
            return
        try:
            self._listen_key = await self._create_listen_key()
            if not self._listen_key:
                logger.error("無法取得 Binance listenKey，將略過 user data stream。")
                return
            logger.info("取得 Binance listenKey 成功，啟動 user data stream。")
            self._user_stream_task = asyncio.create_task(self._user_stream_listener())
            self._listen_key_keepalive_task = asyncio.create_task(self._keepalive_user_stream())
        except Exception as exc:
            logger.error(f"初始化 Binance user data stream 失敗: {exc}")

    async def _create_listen_key(self) -> Optional[str]:
        assert self._session is not None
        async with self._session.post(f"{self.base_url}/api/v3/userDataStream") as response:
            body = await response.text()
            if response.status >= 400:
                logger.error(f"申請 listenKey 失敗 [{response.status}]: {body}")
                return None
            data = orjson.loads(body)
            return data.get('listenKey')

    async def _keepalive_user_stream(self) -> None:
        if not self._session:
            return
        while not self._closing and self._listen_key:
            await asyncio.sleep(USER_STREAM_KEEPALIVE_SECONDS)
            try:
                async with self._session.put(
                    f"{self.base_url}/api/v3/userDataStream",
                    params={'listenKey': self._listen_key}
                ) as response:
                    if response.status >= 400:
                        body = await response.text()
                        logger.warning(f"listenKey keepalive 失敗 [{response.status}]: {body}")
                    else:
                        logger.debug("成功維持 Binance listenKey。")
            except asyncio.CancelledError:  # pragma: no cover
                break
            except Exception as exc:  # pragma: no cover
                logger.warning(f"listenKey keepalive 發生例外: {exc}")

    async def _delete_listen_key(self) -> None:
        if not self._session or not self._listen_key:
            return
        try:
            async with self._session.delete(
                f"{self.base_url}/api/v3/userDataStream",
                params={'listenKey': self._listen_key}
            ) as response:
                if response.status >= 400:
                    body = await response.text()
                    logger.warning(f"刪除 listenKey 失敗 [{response.status}]: {body}")
        except Exception as exc:  # pragma: no cover
            logger.warning(f"刪除 listenKey 發生例外: {exc}")
        finally:
            self._listen_key = None

    async def _user_stream_listener(self) -> None:
        if not self._listen_key:
            return
        url = f"{self.ws_url}/{self._listen_key}"
        while not self._closing:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    logger.info("已連接 Binance User Data Stream。")
                    async for message in ws:
                        data = orjson.loads(message)
                        event_type = data.get('e')
                        if event_type == 'executionReport':
                            await self._process_execution_report(data)
            except asyncio.CancelledError:  # pragma: no cover
                break
            except Exception as exc:
                if not self._closing:
                    logger.error(f"User data stream 發生錯誤: {exc}，5 秒後重試。")
                    await asyncio.sleep(5)

    async def _process_execution_report(self, event: dict) -> None:
        order_id = str(event.get('i'))
        status_raw = str(event.get('X', '')).lower()
        status_map = {
            "new": "partially_filled",
            "partially_filled": "partially_filled",
            "partial_fill": "partially_filled",
            "filled": "filled",
            "canceled": "canceled",
            "expired": "canceled",
            "rejected": "rejected",
        }
        status = status_map.get(status_raw, "partially_filled")

        last_filled = Decimal(str(event.get('l', '0')))
        cumulative_filled = Decimal(str(event.get('z', '0')))
        cumulative_quote = Decimal(str(event.get('Z', '0')))

        avg_fill_price = Decimal('0')
        if cumulative_filled > 0 and cumulative_quote > 0:
            avg_fill_price = cumulative_quote / cumulative_filled
        elif last_filled > 0:
            avg_fill_price = Decimal(str(event.get('L', '0')))

        reason = event.get('r')
        if reason and reason.upper() == 'NONE':
            reason = None

        ts_ms = event.get('E') or event.get('T') or int(time.time() * 1000)
        ts = float(ts_ms) / 1000 if ts_ms else time.time()

        update = OrderUpdate(
            order_id=order_id,
            status=status,
            filled_size=last_filled,
            avg_fill_price=avg_fill_price,
            reason=reason,
            ts=ts,
        )

        pair = self._order_pair_cache.get(order_id)
        if status in {"filled", "canceled", "rejected"}:
            self._order_pair_cache.pop(order_id, None)

        await self._publish_dashboard_fill(event, update, pair)

    async def _publish_dashboard_fill(self, event: dict, update: OrderUpdate, pair: Optional[MarketPair]) -> None:
        if not self.dashboard_publisher:
            return
        try:
            payload = {
                "type": "cex_fill",
                "source": "binance",
                "data": {
                    "order_id": update.order_id,
                    "symbol": pair.cex_symbol if pair else event.get('s'),
                    "side": event.get('S'),
                    "status": update.status,
                    "fill_size": float(update.filled_size),
                    "avg_fill_price": float(update.avg_fill_price) if update.avg_fill_price is not None else 0.0,
                    "cumulative_filled": float(Decimal(str(event.get('z', '0')))),
                    "cumulative_quote": float(Decimal(str(event.get('Z', '0')))),
                    "event_time": event.get('E'),
                }
            }
            await self.dashboard_publisher.publish(payload)
        except Exception as exc:  # pragma: no cover
            logger.error(f"推送成交資訊到 dashboard 失敗: {exc}")

    async def _ws_listener(self):
        streams = [f"{p.cex_symbol.replace('/', '').lower()}@depth" for p in self.pairs]
        url = f"{self.ws_url}/{'/'.join(streams)}"
        
        while True:
            try:
                async with websockets.connect(url) as ws:
                    self._ws_conn = ws
                    logger.info(f"已連接到 Binance WebSocket: {url}")
                    # 在連接後，為每個交易對同步訂單簿
                    await self._sync_all_orderbooks()

                    async for message in ws:
                        data = orjson.loads(message)
                        if "stream" in data:
                            await self._handle_ws_message(data['data'])
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"Binance WebSocket 連線關閉: {e}。5 秒後重連...")
            except Exception as e:
                logger.error(f"WebSocket 監聽發生錯誤: {e}。5 秒後重連...", exc_info=True)
            
            await asyncio.sleep(5)

    async def _sync_all_orderbooks(self):
        for pair in self.pairs:
            await self._sync_orderbook(pair)

    async def _sync_orderbook(self, pair: MarketPair):
        symbol = pair.cex_symbol.replace('/', '')
        logger.debug(f"正在為 {symbol} 同步訂單簿快照...")
        if not self._session:
            raise ConnectionError("Session not initialized.")

        params = {'symbol': symbol, 'limit': 1000}
        try:
            async with self._session.get(f"{self.base_url}/api/v3/depth", params=params) as response:
                response.raise_for_status()
                data = await response.json(loads=orjson.loads)
                
                self.last_update_ids[symbol] = data['lastUpdateId']
                self.orderbooks[symbol]['bids'] = {Decimal(price): Decimal(qty) for price, qty in data['bids']}
                self.orderbooks[symbol]['asks'] = {Decimal(price): Decimal(qty) for price, qty in data['asks']}
                logger.info(f"{symbol} 訂單簿同步完成。LastUpdateId: {data['lastUpdateId']}")
        except aiohttp.ClientError as e:
            logger.error(f"獲取 {symbol} 訂單簿快照失敗: {e}")

    async def _handle_ws_message(self, data: dict):
        if data.get('e') != 'depthUpdate':
            return

        symbol = data['s']
        first_update_id = data['U']
        final_update_id = data['u']

        if symbol not in self.last_update_ids:
            logger.warning(f"收到 {symbol} 的 WebSocket 更新，但訂單簿尚未同步。")
            return

        # 根據 Binance 文件，如果 first_update_id > last_update_id + 1，需要重新同步
        if first_update_id > self.last_update_ids[symbol] + 1:
            logger.warning(f"{symbol} WebSocket 更新脫節，正在重新同步...")
            await self._sync_orderbook(next(p for p in self.pairs if p.cex_symbol.replace('/','') == symbol))
            return

        # 只有當 final_update_id > last_update_id 時才處理更新
        if final_update_id <= self.last_update_ids[symbol]:
            return

        # 更新訂單簿
        for price, qty in data['b']: # Bids
            p, q = Decimal(price), Decimal(qty)
            if q == 0:
                self.orderbooks[symbol]['bids'].pop(p, None)
            else:
                self.orderbooks[symbol]['bids'][p] = q
        
        for price, qty in data['a']: # Asks
            p, q = Decimal(price), Decimal(qty)
            if q == 0:
                self.orderbooks[symbol]['asks'].pop(p, None)
            else:
                self.orderbooks[symbol]['asks'][p] = q
        
        self.last_update_ids[symbol] = final_update_id

    async def get_quote(self, pair: MarketPair) -> Optional[Quote]:
        symbol = pair.cex_symbol.replace('/', '')
        book = self.orderbooks.get(symbol)

        if book and book['bids'] and book['asks']:
            # 優先使用本地訂單簿
            best_bid = max(book['bids'].keys())
            best_ask = min(book['asks'].keys())
            bid_size = book['bids'][best_bid]
            ask_size = book['asks'][best_ask]
        else:
            # 如果本地訂單簿不可用，透過 API 直接查詢
            logger.info(f"本地訂單簿 {symbol} 不可用，正在透過 API 查詢報價...")
            if not self._session:
                logger.error("Session not initialized for API quote fetch.")
                return None
            
            params = {'symbol': symbol}
            try:
                async with self._session.get(f"{self.base_url}/api/v3/ticker/bookTicker", params=params) as response:
                    response.raise_for_status()
                    data = await response.json(loads=orjson.loads)
                    best_bid = Decimal(data['bidPrice'])
                    best_ask = Decimal(data['askPrice'])
                    bid_size = Decimal(data['bidQty'])
                    ask_size = Decimal(data['askQty'])
                    logger.info(f"成功透過 API 獲取 {symbol} 報價: Bid={best_bid}, Ask={best_ask}")
            except aiohttp.ClientError as e:
                logger.error(f"透過 API 獲取 {symbol} 報價失敗: {e}")
                return None
            except Exception as e:
                logger.error(f"處理 API 報價時發生未知錯誤: {e}", exc_info=True)
                return None

        return Quote(
            pair=pair,
            price=(best_bid + best_ask) / 2,
            bid_price=best_bid,
            ask_price=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            side="buy", # 佔位
            size=Decimal(0), # 佔位
            venue="CEX",
            timestamp=asyncio.get_running_loop().time()
        )

    async def create_order(self, order: CexOrder) -> OrderUpdate:
        if not self._session:
            raise ConnectionError("Session not initialized.")

        params = {
            'symbol': order.pair.cex_symbol.replace('/', ''),
            'side': order.side.upper(),
            'type': order.type.upper(),
            'quantity': f"{order.size:.{order.pair.base_precision}f}",
            'timestamp': int(time.time() * 1000)
        }
        if order.type == 'LIMIT':
            params['price'] = f"{order.price:.{order.pair.quote_precision}f}"
            params['timeInForce'] = 'GTC'

        params['signature'] = self._get_signature(params)
        
        try:
            async with self._session.post(f"{self.base_url}/api/v3/order", data=params) as response:
                body = await response.text()
                if response.status >= 400:
                    logger.error(f"Binance下單失敗 [{response.status}]: {body}")
                    raise aiohttp.ClientResponseError(
                        response.request_info,
                        response.history,
                        status=response.status,
                        message=body,
                        headers=response.headers,
                    )
                
                data = orjson.loads(body)
                logger.info("CEX 訂單已創建: %s", data)

                status_raw = str(data.get('status', '')).lower()
                status_map = {
                    "new": "partially_filled",
                    "partially_filled": "partially_filled",
                    "partial_fill": "partially_filled",
                    "partial_filled": "partially_filled",
                    "filled": "filled",
                    "canceled": "canceled",
                    "expired": "canceled",
                    "rejected": "rejected",
                }
                status = status_map.get(status_raw, "partially_filled")

                filled_size = Decimal(str(data.get('executedQty', '0')))
                avg_price_value = data.get('avgPrice') or data.get('price') or '0'
                avg_fill_price = Decimal(str(avg_price_value))
                ts_ms = data.get('transactTime') or data.get('updateTime') or int(time.time() * 1000)
                ts = float(ts_ms) / 1000 if ts_ms else time.time()

                order_id = str(data['orderId'])
                self._order_pair_cache[order_id] = order.pair

                return OrderUpdate(
                    order_id=str(data['orderId']),
                    status=status,
                    avg_fill_price=avg_fill_price,
                    filled_size=filled_size,
                    ts=ts,
                )
        except aiohttp.ClientResponseError as e:
            logger.error("創建 CEX 訂單失敗: [%s] %s", e.status, e.message)
            raise

    async def get_balance(self, asset: str) -> Decimal:
        """查詢特定資產的可用餘額。"""
        # CEX (Binance) 通常使用 ETH 而不是 WETH
        query_asset = "ETH" if asset.upper() == "WETH" else asset
        
        logger.debug(f"正在查詢 CEX 資產 {asset} (查詢為 {query_asset}) 的餘額...")
        params = {
            'timestamp': int(time.time() * 1000),
            'recvWindow': self.config.recv_window_ms
        }
        params['signature'] = self._get_signature(params)
        try:
            async with self._session.get(f"{self.base_url}/api/v3/account", params=params) as response:
                data = await response.json(loads=orjson.loads)
                response.raise_for_status()
                
                for balance in data['balances']:
                    if balance['asset'] == query_asset:
                        return Decimal(balance['free'])
                return Decimal("0") # 如果餘額列表中沒有該資產
        except Exception as e:
            logger.error(f"查詢 CEX 餘額失敗: {e}")
            return Decimal("-1") # 返回負值表示查詢出錯

    async def cancel_order(self, order_id: str, pair: MarketPair) -> bool:
        # 待實現: 取消訂單邏輯
        logger.info(f"模擬取消訂單: {order_id}")
        return True
