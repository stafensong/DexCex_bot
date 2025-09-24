import orjson
from typing import Optional
from loguru import logger

try:
    from redis.asyncio import Redis
except ImportError:  # pragma: no cover
    Redis = None  # type: ignore


class DashboardPublisher:
    """負責將資料推送到 Redis，供 dashboard 後端轉發。"""

    def __init__(self, redis_url: str, channel: str) -> None:
        if Redis is None:
            raise RuntimeError("redis 套件未安裝，無法啟用 DashboardPublisher")
        self._redis: Redis = Redis.from_url(redis_url, encoding="utf-8", decode_responses=True)
        self._channel = channel

    async def publish(self, payload: dict) -> None:
        try:
            message = orjson.dumps(payload).decode("utf-8")
            await self._redis.publish(self._channel, message)
        except Exception as exc:  # pragma: no cover
            logger.error(f"推送 dashboard 訊息失敗: {exc}")

    async def close(self) -> None:
        try:
            await self._redis.close()
        except Exception as exc:  # pragma: no cover
            logger.warning(f"關閉 dashboard Redis 連線失敗: {exc}")
