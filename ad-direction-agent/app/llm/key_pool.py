"""API Key 池 — 轮询分发 + 失败感知冷却

用法:
    pool = ApiKeyPool(["sk-key1", "sk-key2", "sk-key3"])
    key = pool.next_key()
    pool.mark_failed(key, status_code=429)   # 限流 → 冷却 60s
    pool.mark_failed(key, status_code=401)   # 无效 → 冷却 1h
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)

COOLDOWN_RATE_LIMIT = 60      # 429 限流
COOLDOWN_AUTH_ERROR = 3600    # 401/403 无效 Key
COOLDOWN_OTHER = 30           # 其他临时错误


class ApiKeyPool:
    """多 Key 轮询池

    线程安全（threading.Lock），兼容 asyncio 上下文。
    全部 key 被冷却时降级返回冷却时间最短的 key。
    """

    def __init__(self, keys: list[str]):
        if not keys:
            raise ValueError("ApiKeyPool 至少需要一个 key")
        self._keys = list(keys)
        self._index = 0
        self._cooldowns: dict[str, float] = {}  # key → 冷却结束时间戳
        self._lock = threading.Lock()

    @property
    def key_count(self) -> int:
        return len(self._keys)

    @property
    def healthy_count(self) -> int:
        now = time.time()
        return sum(1 for k in self._keys if self._cooldowns.get(k, 0) <= now)

    def next_key(self) -> str:
        """轮询获取下一个可用 key（跳过冷却中的）"""
        with self._lock:
            n = len(self._keys)
            now = time.time()
            for _ in range(n):
                self._index = (self._index + 1) % n
                key = self._keys[self._index]
                if self._cooldowns.get(key, 0) <= now:
                    return key
            # 全部在冷却中 → 取冷却剩余最短的
            best = min(self._keys, key=lambda k: self._cooldowns.get(k, 0))
            remaining = self._cooldowns.get(best, 0) - now
            logger.warning(
                "全部 %d 个 key 都在冷却中，降级使用 %s...（剩余 %.1fs）",
                n, best[-8:], max(remaining, 0),
            )
            return best

    def mark_failed(self, key: str, status_code: int = 0):
        """根据错误类型标记冷却时长"""
        if status_code in (401, 403):
            cooldown = COOLDOWN_AUTH_ERROR
        elif status_code == 429:
            cooldown = COOLDOWN_RATE_LIMIT
        else:
            cooldown = COOLDOWN_OTHER
        with self._lock:
            until = time.time() + cooldown
            self._cooldowns[key] = until
            logger.info(
                "Key ...%s 失败(HTTP %d)，冷却 %ds 至 %s",
                key[-8:], status_code, cooldown,
                time.strftime("%H:%M:%S", time.localtime(until)),
            )

    def mark_success(self, key: str):
        """清除冷却（成功调用时加速恢复）"""
        with self._lock:
            self._cooldowns.pop(key, None)
