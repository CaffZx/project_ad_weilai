"""app.data.starrocks_retry 单测：BE 存储错判定 + 同步重试包装。"""
from __future__ import annotations

from unittest.mock import patch

import pymysql
import pytest

from app.data.starrocks_retry import (
    STARROCKS_BE_RETRIES,
    is_starrocks_be_storage_error,
    run_sync_with_be_retry,
)


def _be_err() -> pymysql.err.ProgrammingError:
    return pymysql.err.ProgrammingError(
        1064,
        "starlet err Create hdfs root dir '...' error: Read-only file system ... BE:10064",
    )


def _cache_err() -> pymysql.err.ProgrammingError:
    return pymysql.err.ProgrammingError(
        1064, "starlet err Can't allocate cache directory for hdfs://...: BE:10062"
    )


def _syntax_err() -> pymysql.err.ProgrammingError:
    return pymysql.err.ProgrammingError(
        1064, "Getting syntax error at line 1. Detail message: No viable statement"
    )


def test_is_starrocks_be_storage_error():
    assert is_starrocks_be_storage_error(_be_err()) is True
    assert is_starrocks_be_storage_error(_cache_err()) is True
    # 同为 1064 的 SQL 语法错 → 不算 BE 存储错，不该重试
    assert is_starrocks_be_storage_error(_syntax_err()) is False
    # 连接级错（不同 errno/类型）→ 不算
    assert is_starrocks_be_storage_error(
        pymysql.err.OperationalError(2013, "Lost connection")
    ) is False
    assert is_starrocks_be_storage_error(ValueError("x")) is False


def test_run_sync_retries_be_error_then_succeeds():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _be_err()
        return "ok"

    with patch("app.data.starrocks_retry.time.sleep") as msleep:
        assert run_sync_with_be_retry(fn, label="t") == "ok"
    assert calls["n"] == 3                    # 主 + 2 次重试
    assert msleep.call_count == 2


def test_run_sync_no_retry_on_syntax_error():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _syntax_err()

    with patch("app.data.starrocks_retry.time.sleep") as msleep:
        with pytest.raises(pymysql.err.ProgrammingError):
            run_sync_with_be_retry(fn)
    assert calls["n"] == 1                    # 立即抛，不重试
    assert msleep.call_count == 0


def test_run_sync_gives_up_after_retries():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _be_err()

    with patch("app.data.starrocks_retry.time.sleep") as msleep:
        with pytest.raises(pymysql.err.ProgrammingError):
            run_sync_with_be_retry(fn)
    assert calls["n"] == 1 + STARROCKS_BE_RETRIES   # 共 3 次
    assert msleep.call_count == STARROCKS_BE_RETRIES


def test_mcp_db_context_lookup_is_wired_with_retry():
    """#1/#3 的 SQL 地基 mcp_db_context._lookup_sync 必须真的接入了 BE 重试。"""
    from app.data import mcp_db_context as m

    attempts = {"n": 0}

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            attempts["n"] += 1
            if attempts["n"] <= 2:           # 前两次抛 BE 错，第三次成功
                raise _be_err()

        def fetchone(self):
            return {"parent_asin": "X", "parent_seller_sku": "SKU", "shop_account": "shop"}

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    with patch.object(m, "_db_connect_kwargs", return_value={"host": "h"}), \
         patch("app.data.mcp_db_context.pymysql.connect", return_value=_Conn()), \
         patch("app.data.starrocks_retry.time.sleep"):
        row = m._lookup_sync("X", None)

    assert row["parent_seller_sku"] == "SKU"
    assert attempts["n"] == 3                 # 主 + 2 次重试 → 证明已接入重试


def test_mcp_db_context_top_child_is_wired_with_retry():
    """另一条裸查询 lookup_top_child_attrs 同样必须接入 BE 重试（隐患1 补齐）。"""
    from app.data import mcp_db_context as m

    calls = {"n": 0}

    class _Cur:
        _last = ""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            calls["n"] += 1
            if calls["n"] <= 2:              # 前两次（均为 step1）抛 BE 错 → 触发两次重试
                raise _be_err()
            self._last = sql

        def fetchall(self):                  # step1 子 ASIN 列表
            return [{"asin": "CHILD1"}]

        def fetchone(self):
            if "SUM(cost)" in self._last:    # step2/3 取 top
                return {"asin": "CHILD1", "s": 10}
            return {"product_size": "M", "product_color": "Red"}  # 最后取 size/color

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    with patch.object(m, "_db_connect_kwargs", return_value={"host": "h"}), \
         patch("app.data.mcp_db_context.pymysql.connect", side_effect=lambda **k: _Conn()), \
         patch("app.data.starrocks_retry.time.sleep") as msleep:
        res = m.lookup_top_child_attrs("PARENT")

    assert res and res["asin"] == "CHILD1"
    assert msleep.call_count == 2             # 重试 2 次 → 证明已接入重试
