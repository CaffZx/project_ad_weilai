"""DbAdapter._query 针对 StarRocks BE 存储错误的重试逻辑单测。

只重试 errno 1064 且带 starlet/BE: 签名的存储错（如 BE:10064 只读 FS、
BE:10062 cache 目录分配失败）；真正的 SQL 语法错（同 1064 无签名）不重试。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pymysql
import pytest

from app.data.db_adapter import DbAdapter, _is_starrocks_be_storage_error


def _be_err() -> pymysql.err.ProgrammingError:
    return pymysql.err.ProgrammingError(
        1064,
        "starlet err Create hdfs root dir 'db.../...' error: "
        "Read-only file system: Read-only ... BE:10064",
    )


def _cache_err() -> pymysql.err.ProgrammingError:
    return pymysql.err.ProgrammingError(
        1064, "starlet err Can't allocate cache directory for hdfs://...: BE:10062"
    )


def _syntax_err() -> pymysql.err.ProgrammingError:
    return pymysql.err.ProgrammingError(
        1064, "Getting syntax error at line 1. Detail message: No viable statement"
    )


def _make_adapter() -> DbAdapter:
    """绕过 __init__（不建真实连接池），只装回 _query 需要的信号量。"""
    a = DbAdapter.__new__(DbAdapter)
    a._query_sem = asyncio.Semaphore(8)
    return a


def test_is_starrocks_be_storage_error():
    assert _is_starrocks_be_storage_error(_be_err()) is True
    assert _is_starrocks_be_storage_error(_cache_err()) is True
    # 同为 1064 的 SQL 语法错 → 不算 BE 存储错
    assert _is_starrocks_be_storage_error(_syntax_err()) is False
    # 连接级错（不同 errno/类型）→ 不算
    assert _is_starrocks_be_storage_error(
        pymysql.err.OperationalError(2013, "Lost connection")
    ) is False
    assert _is_starrocks_be_storage_error(ValueError("x")) is False


def test_query_retries_on_be_error_then_succeeds():
    """BE 错 2 次后成功 → 返回结果，sleep 被调用 2 次。"""
    a = _make_adapter()
    calls = {"n": 0}

    async def fake_to_thread(fn, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _be_err()
        return [{"x": 1}]

    with patch("app.data.db_adapter.asyncio.to_thread", new=fake_to_thread), \
         patch("app.data.db_adapter.asyncio.sleep", new_callable=AsyncMock) as msleep:
        res = asyncio.run(a._query("SELECT 1", label="t"))

    assert res == [{"x": 1}]
    assert calls["n"] == 3          # 主 + 2 次重试
    assert msleep.await_count == 2


def test_query_no_retry_on_syntax_error():
    """SQL 语法错（1064 无 starlet 签名）→ 只尝试 1 次、立即抛。"""
    a = _make_adapter()
    calls = {"n": 0}

    async def fake_to_thread(fn, *args, **kwargs):
        calls["n"] += 1
        raise _syntax_err()

    with patch("app.data.db_adapter.asyncio.to_thread", new=fake_to_thread), \
         patch("app.data.db_adapter.asyncio.sleep", new_callable=AsyncMock) as msleep:
        with pytest.raises(pymysql.err.ProgrammingError):
            asyncio.run(a._query("SELECT bad"))

    assert calls["n"] == 1
    assert msleep.await_count == 0


def test_query_gives_up_after_retries():
    """BE 错持续 → 用尽重试后抛出，共尝试 3 次、sleep 2 次。"""
    a = _make_adapter()
    calls = {"n": 0}

    async def fake_to_thread(fn, *args, **kwargs):
        calls["n"] += 1
        raise _be_err()

    with patch("app.data.db_adapter.asyncio.to_thread", new=fake_to_thread), \
         patch("app.data.db_adapter.asyncio.sleep", new_callable=AsyncMock) as msleep:
        with pytest.raises(pymysql.err.ProgrammingError):
            asyncio.run(a._query("SELECT 1"))

    assert calls["n"] == 3          # 1 + STARROCKS_BE_RETRIES(2)
    assert msleep.await_count == 2
