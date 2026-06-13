"""Redis 短期 ASIN 数据缓存 + 进程内 LRU 兜底。"""

from __future__ import annotations

import hashlib
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
    """asin_data:{asin}:{days}[:f<hash>] — 完整 2h；partial — 15min；Redis 不可用时 30s LRU。

    meta_filter 维度：各层按场景裁剪 META 列表（tactics/diagnosis/p3/execution 各不同），
    各自独立缓存互不污染。key 后缀 :f<hash> = 归一化(排序去重) filter 列表的 md5 前 10 位；
    meta_filter 为空 = 全量数据，无后缀。全量是任意 filter 的超集，可被各层复用（见 _ensure_data）。
    """

    def __init__(self):
        self._lru: dict[tuple[str, int, str], _LruEntry] = {}
        self._redis = None
        self._redis_ok: bool | None = None

    @staticmethod
    def _filter_suffix(meta_filter: list[str] | None) -> str:
        if not meta_filter:
            return ""
        norm = ",".join(sorted(set(meta_filter)))
        return ":f" + hashlib.md5(norm.encode("utf-8")).hexdigest()[:10]

    def _key(self, asin: str, days: int, partial: bool = False,
             meta_filter: list[str] | None = None) -> str:
        prefix = "asin_data_partial" if partial else "asin_data"
        return f"{prefix}:{asin}:{days}{self._filter_suffix(meta_filter)}"

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
                socket_connect_timeout=3,   # 连接建立超时(秒)
                socket_timeout=5,           # 单次读写超时:Redis 卡住 5s 快速失败→走 LRU 兜底,不挂死
            )
            await self._redis.ping()
            self._redis_ok = True
            logger.info("Redis cache connected")
            return self._redis
        except Exception as e:  # noqa: BLE001
            logger.warning("Redis 不可用，使用进程内 LRU: %s", e)
            self._redis_ok = False
            return None

    async def get(self, asin: str, days: int = 7,
                  meta_filter: list[str] | None = None) -> ASINData | None:
        lru_key = (asin, days, self._filter_suffix(meta_filter))
        r = await self._get_redis()
        if r:
            try:
                raw = await r.get(self._key(asin, days, meta_filter=meta_filter))
                if raw:
                    data = ASINData.model_validate_json(raw)
                    data.data_freshness = getattr(data, "data_freshness", None) or "fresh"
                    return data
                partial_raw = await r.get(self._key(asin, days, partial=True, meta_filter=meta_filter))
                if partial_raw:
                    data = ASINData.model_validate_json(partial_raw)
                    data.data_freshness = "partial"
                    return data
            except Exception as e:  # noqa: BLE001
                logger.warning("Redis get failed [%s]: %s", asin, e)
        entry = self._lru.get(lru_key)
        if entry and entry.expires_at > time.time():
            return entry.data
        return None

    async def set(self, asin: str, data: ASINData, days: int = 7,
                  meta_filter: list[str] | None = None) -> None:
        partial = bool(data.partial_failures)
        ttl = settings.redis_partial_ttl if partial else settings.redis_short_ttl
        lru_key = (asin, days, self._filter_suffix(meta_filter))
        lru_ttl = settings.redis_lru_ttl
        self._lru[lru_key] = _LruEntry(data=data, expires_at=time.time() + lru_ttl)

        r = await self._get_redis()
        if not r:
            return
        try:
            payload = data.model_dump_json()
            if partial:
                await r.setex(self._key(asin, days, partial=True, meta_filter=meta_filter), ttl, payload)
            else:
                await r.setex(self._key(asin, days, meta_filter=meta_filter), ttl, payload)
                await r.delete(self._key(asin, days, partial=True, meta_filter=meta_filter))
        except Exception as e:  # noqa: BLE001
            logger.warning("Redis set failed [%s]: %s", asin, e)

    async def delete(self, asin: str, days: int = 7,
                     meta_filter: list[str] | None = None) -> None:
        self._lru.pop((asin, days, self._filter_suffix(meta_filter)), None)
        r = await self._get_redis()
        if not r:
            return
        try:
            await r.delete(self._key(asin, days, meta_filter=meta_filter))
            await r.delete(self._key(asin, days, partial=True, meta_filter=meta_filter))
        except Exception as e:  # noqa: BLE001
            logger.warning("Redis delete failed [%s]: %s", asin, e)


# 全局单例
asin_data_cache = AsinDataCache()
