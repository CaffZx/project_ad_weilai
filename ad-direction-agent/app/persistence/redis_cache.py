"""Redis 短期 ASIN 数据缓存 + 进程内 LRU 兜底。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.config.settings import settings
from app.models.asin_data import ASINData

logger = logging.getLogger(__name__)


@dataclass
class _LruEntry:
    data: ASINData
    expires_at: float


class AsinDataCache:
    """asin_data:{asin}:{days} — 完整 2h；partial — 15min；Redis 不可用时 30s LRU。"""

    def __init__(self):
        self._lru: dict[tuple[str, int], _LruEntry] = {}
        self._redis = None
        self._redis_ok: bool | None = None

    def _key(self, asin: str, days: int, partial: bool = False) -> str:
        prefix = "asin_data_partial" if partial else "asin_data"
        return f"{prefix}:{asin}:{days}"

    async def _get_redis(self):
        if not settings.redis_enabled:
            return None
        if self._redis_ok is False:
            return None
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            await self._redis.ping()
            self._redis_ok = True
            logger.info("Redis cache connected")
            return self._redis
        except Exception as e:  # noqa: BLE001
            logger.warning("Redis 不可用，使用进程内 LRU: %s", e)
            self._redis_ok = False
            return None

    async def get(self, asin: str, days: int = 7) -> ASINData | None:
        key = (asin, days)
        r = await self._get_redis()
        if r:
            try:
                raw = await r.get(self._key(asin, days))
                if raw:
                    data = ASINData.model_validate_json(raw)
                    data.data_freshness = getattr(data, "data_freshness", None) or "fresh"
                    return data
                partial_raw = await r.get(self._key(asin, days, partial=True))
                if partial_raw:
                    data = ASINData.model_validate_json(partial_raw)
                    data.data_freshness = "partial"
                    return data
            except Exception as e:  # noqa: BLE001
                logger.warning("Redis get failed [%s]: %s", asin, e)
        entry = self._lru.get(key)
        if entry and entry.expires_at > time.time():
            return entry.data
        return None

    async def set(self, asin: str, data: ASINData, days: int = 7) -> None:
        partial = bool(data.partial_failures)
        ttl = settings.redis_partial_ttl if partial else settings.redis_short_ttl
        key = (asin, days)
        lru_ttl = settings.redis_lru_ttl
        self._lru[key] = _LruEntry(data=data, expires_at=time.time() + lru_ttl)

        r = await self._get_redis()
        if not r:
            return
        try:
            payload = data.model_dump_json()
            if partial:
                await r.setex(self._key(asin, days, partial=True), ttl, payload)
            else:
                await r.setex(self._key(asin, days), ttl, payload)
                await r.delete(self._key(asin, days, partial=True))
        except Exception as e:  # noqa: BLE001
            logger.warning("Redis set failed [%s]: %s", asin, e)

    async def delete(self, asin: str, days: int = 7) -> None:
        self._lru.pop((asin, days), None)
        r = await self._get_redis()
        if not r:
            return
        try:
            await r.delete(self._key(asin, days))
            await r.delete(self._key(asin, days, partial=True))
        except Exception as e:  # noqa: BLE001
            logger.warning("Redis delete failed [%s]: %s", asin, e)


# 全局单例
asin_data_cache = AsinDataCache()
