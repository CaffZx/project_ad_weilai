"""共享 Redis 客户端 + 分布式互斥锁辅助。

懒加载单例 + 熔断（对齐 redis_cache.AsinDataCache 模式：redis_enabled 门 + _redis_ok 熔断）。
全服务需要跨进程互斥锁、或共享 Redis 连接的地方都应复用本模块的 get_redis()，
避免各模块各自 from_url 造成连接风暴。

锁语义：
- acquire_lock 用 SET NX EX；Redis 不可用时降级进程内 dict（仅单 worker 有效，不崩）。
- release_lock 用 compare-and-delete（Lua）：仅当值 == 本 token 才删，
  防止 A 超时后 B 抢到锁、A 的 finally 误删 B 的锁。
"""

from __future__ import annotations

import logging
import time

from app.config.settings import settings

logger = logging.getLogger(__name__)

_redis_client = None
_redis_ok: bool | None = None


async def get_redis():
    """懒加载共享 Redis 客户端；未启用/不可用时返回 None（熔断，调用方自行兜底）。"""
    global _redis_client, _redis_ok
    if not settings.redis_enabled:
        return None
    if _redis_ok is False:
        return None
    if _redis_client is not None:
        return _redis_client
    try:
        import redis.asyncio as aioredis

        _redis_client = aioredis.from_url(
            settings.redis_url, encoding="utf-8", decode_responses=True
        )
        await _redis_client.ping()
        _redis_ok = True
        logger.info("Shared Redis 已连接")
        return _redis_client
    except Exception as e:  # noqa: BLE001
        logger.warning("Shared Redis 不可用，调用方降级（锁退进程内，仅单 worker 有效）: %s", e)
        _redis_ok = False
        return None


# 仅当值 == 本 token 才删，防止误删他人锁。
_RELEASE_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

# 进程内锁兜底（Redis 不可用时）：key → (token, monotonic_acquired)
_local_locks: dict[str, tuple[str, float]] = {}


async def acquire_lock(key: str, token: str, ttl: int) -> tuple[bool, str | None]:
    """获取互斥锁。返回 (是否成功, 当前持有者 token 或 None)。

    ttl 单位秒，必须 ≥ 持锁任务的最大时长，否则锁中途过期会被他人抢占。
    """
    r = await get_redis()
    if r is not None:
        try:
            ok = await r.set(key, token, nx=True, ex=ttl)
            if ok:
                return True, None
            holder = await r.get(key)
            return False, holder
        except Exception as e:  # noqa: BLE001
            logger.warning("acquire_lock 异常 [%s]，降级进程内: %s", key, e)
    # 进程内兜底（单 worker）
    now = time.monotonic()
    existing = _local_locks.get(key)
    if existing and (now - existing[1]) < ttl:
        return False, existing[0]
    _local_locks[key] = (token, now)
    return True, None


async def release_lock(key: str, token: str) -> None:
    """释放互斥锁。仅释放本 token 持有的锁（compare-and-delete）。"""
    r = await get_redis()
    if r is not None:
        try:
            await r.eval(_RELEASE_LOCK_LUA, 1, key, token)
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("release_lock 异常 [%s]: %s", key, e)
    existing = _local_locks.get(key)
    if existing and existing[0] == token:
        _local_locks.pop(key, None)
