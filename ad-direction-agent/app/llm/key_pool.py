"""API Key 池 — 轮询分发 + 失败感知冷却 + 状态落盘 + 统计

用法:
    pool = ApiKeyPool(["sk-key1", "sk-key2"], state_path="config/key_pool_state.local.json")
    key = pool.next_key()
    pool.mark_failed(key, status_code=429)   # 限流 → 冷却 60s
    pool.mark_success(key, latency=1.2)
    pool.stats()
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

COOLDOWN_RATE_LIMIT = 60      # 429 限流
COOLDOWN_AUTH_ERROR = 3600    # 401/403 无效 Key
COOLDOWN_OTHER = 30           # 其他临时错误
STATS_LOG_INTERVAL = 50       # 每 N 次调用输出一次 stats 摘要


def _empty_stat() -> dict:
    return {
        "success": 0,
        "fail": 0,
        "total_latency": 0.0,
        "cooldown_count": 0,
    }


class ApiKeyPool:
    """多 Key 轮询池

    线程安全（threading.Lock），兼容 asyncio 上下文。
    全部 key 被冷却时降级返回冷却时间最短的 key。
    冷却状态可落盘到 JSON，重启后恢复未过期的冷却。
    """

    def __init__(self, keys: list[str], state_path: str | None = None):
        if not keys:
            raise ValueError("ApiKeyPool 至少需要一个 key")
        self._keys = list(keys)
        self._index = 0
        self._cooldowns: dict[str, float] = {}  # key → 冷却结束时间戳
        self._stats: dict[str, dict] = {k: _empty_stat() for k in self._keys}
        self._call_count = 0
        self._state_path = state_path
        self._lock = threading.RLock()
        if state_path:
            self._load_state()

    @property
    def key_count(self) -> int:
        return len(self._keys)

    @property
    def healthy_count(self) -> int:
        now = time.time()
        return sum(1 for k in self._keys if self._cooldowns.get(k, 0) <= now)

    def _load_state(self) -> None:
        path = Path(self._state_path) if self._state_path else None
        if not path or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            now = time.time()
            cooldowns = data.get("cooldowns", {})
            restored = 0
            for key, until in cooldowns.items():
                if key in self._keys and until > now:
                    self._cooldowns[key] = float(until)
                    restored += 1
            if restored:
                logger.info("从 %s 恢复 %d 个 key 的冷却状态", path, restored)
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
            logger.warning("加载 KeyPool 状态失败 (%s): %s", path, e)

    def _save_state(self) -> None:
        if not self._state_path:
            return
        path = Path(self._state_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"cooldowns": self._cooldowns, "updated_at": time.time()}
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            logger.warning("保存 KeyPool 状态失败 (%s): %s", path, e)

    def _maybe_log_stats(self) -> None:
        if self._call_count % STATS_LOG_INTERVAL == 0 or self.healthy_count == 0:
            s = self.stats()
            logger.info(
                "KeyPool stats: healthy=%d/%d calls=%d",
                s["healthy_count"], s["key_count"], self._call_count,
            )
            for suffix, info in s["keys"].items():
                if info["fail"] > 0 or info["in_cooldown"]:
                    logger.info(
                        "  ...%s ok=%d fail=%d avg_latency=%.2fs cooldown=%s",
                        suffix, info["success"], info["fail"],
                        info["avg_latency_sec"], info["in_cooldown"],
                    )

    def next_key(self) -> str:
        """轮询获取下一个可用 key（跳过冷却中的）"""
        with self._lock:
            self._call_count += 1
            n = len(self._keys)
            now = time.time()
            for _ in range(n):
                self._index = (self._index + 1) % n
                key = self._keys[self._index]
                if self._cooldowns.get(key, 0) <= now:
                    self._maybe_log_stats()
                    return key
            # 全部在冷却中 → 取冷却剩余最短的
            best = min(self._keys, key=lambda k: self._cooldowns.get(k, 0))
            remaining = self._cooldowns.get(best, 0) - now
            logger.warning(
                "全部 %d 个 key 都在冷却中，降级使用 %s...（剩余 %.1fs）",
                n, best[-8:], max(remaining, 0),
            )
            self._maybe_log_stats()
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
            st = self._stats.setdefault(key, _empty_stat())
            st["fail"] += 1
            st["cooldown_count"] += 1
            self._save_state()
            logger.info(
                "Key ...%s 失败(HTTP %d)，冷却 %ds 至 %s",
                key[-8:], status_code, cooldown,
                time.strftime("%H:%M:%S", time.localtime(until)),
            )

    def mark_success(self, key: str, latency: float | None = None):
        """清除冷却（成功调用时加速恢复），可选记录延迟"""
        with self._lock:
            self._cooldowns.pop(key, None)
            st = self._stats.setdefault(key, _empty_stat())
            st["success"] += 1
            if latency is not None:
                st["total_latency"] += latency
            self._save_state()

    def stats(self) -> dict:
        """返回池子与各 key 的运行时统计（供日志/调试）"""
        now = time.time()
        with self._lock:
            keys_info = {}
            for k in self._keys:
                st = self._stats.get(k, _empty_stat())
                ok = st["success"]
                avg = (st["total_latency"] / ok) if ok > 0 else 0.0
                until = self._cooldowns.get(k, 0)
                in_cd = until > now
                keys_info[k[-8:]] = {
                    "success": ok,
                    "fail": st["fail"],
                    "avg_latency_sec": round(avg, 3),
                    "cooldown_count": st["cooldown_count"],
                    "in_cooldown": in_cd,
                    "cooldown_remaining_sec": round(max(until - now, 0), 1) if in_cd else 0,
                }
            return {
                "key_count": len(self._keys),
                "healthy_count": sum(
                    1 for k in self._keys if self._cooldowns.get(k, 0) <= now
                ),
                "total_calls": self._call_count,
                "keys": keys_info,
            }
