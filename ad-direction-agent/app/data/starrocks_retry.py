"""StarRocks 共享存储 BE 故障的重试工具（单一真源）。

被两条数仓 SQL 路径共用：
- `db_adapter._query`（异步：asyncio.sleep 退避）—— 复用本模块的判定与退避计算；
- `mcp_db_context._lookup_sync`（同步裸 pymysql：time.sleep 退避）—— 用 `run_sync_with_be_retry`。

仅针对 starlet/BE 存储错（errno 1064 但带 starlet/BE: 签名，如只读 FS BE:10064、
cache 目录分配失败 BE:10062）做有限重试；真正的 SQL 语法错（同 1064 无签名）不重试。
"""
from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

import pymysql

logger = logging.getLogger(__name__)

STARROCKS_BE_RETRIES = 2            # 额外重试 2 次（共 3 次尝试）
STARROCKS_BE_BACKOFF = (0.5, 1.0)  # 指数退避基值(秒)：第1次重试0.5s、第2次1.0s，另叠加 50–200ms 抖动

T = TypeVar("T")


def is_starrocks_be_storage_error(e: BaseException) -> bool:
    """是否为 StarRocks 共享存储 BE 故障（只读文件系统 / cache 目录分配失败等）。

    这类错误回来是 errno 1064 的 ProgrammingError，但带 starlet/BE: 签名；
    与真正的 SQL 语法错误（同为 1064）区分——后者不应重试。
    """
    if not isinstance(e, pymysql.err.ProgrammingError):
        return False
    args = getattr(e, "args", ())
    if not args or args[0] != 1064:
        return False
    msg = str(e)
    return "starlet err" in msg or "BE:1006" in msg


def backoff_delay(attempt: int) -> float:
    """第 attempt 次重试(0 基)的退避秒数：基值(随 attempt 递增) + 50–200ms 抖动。"""
    base = STARROCKS_BE_BACKOFF[min(attempt, len(STARROCKS_BE_BACKOFF) - 1)]
    return base + random.uniform(0.05, 0.20)


def run_sync_with_be_retry(fn: Callable[[], T], *, label: str = "") -> T:
    """同步执行 fn；命中 StarRocks BE 存储错时按退避+抖动重试（共 1+RETRIES 次）。

    供 mcp_db_context 等「裸 pymysql 同步查询」复用（它们不经过 db_adapter._query）。
    非 BE 存储错（含 SQL 语法错 / 连接错 / 超时）立即抛，不重试。
    fn 每次应自带连接生命周期（重试时用全新连接，勿复用可能已坏的连接）。
    """
    tag = f"[{label}] " if label else ""
    for attempt in range(STARROCKS_BE_RETRIES + 1):
        try:
            return fn()
        except Exception as e:
            if attempt < STARROCKS_BE_RETRIES and is_starrocks_be_storage_error(e):
                delay = backoff_delay(attempt)
                logger.warning(
                    "%sStarRocks BE 存储错误，%.0fms 后重试 (第 %d/%d 次): %s",
                    tag, delay * 1000, attempt + 1, STARROCKS_BE_RETRIES, str(e)[:160],
                )
                time.sleep(delay)
                continue
            raise
