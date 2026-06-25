"""回归：_upsert_ai_suggest 写数值列时，空/None 必须落 NULL 而非 ''。

bug：suggest_budget=decimal(12,2) 遇 '' → MySQL 1366「Incorrect decimal value: ''」，
当 budget_realloc 走 no_budget/none 等兜底分支（budget_bid 无 suggested）时 100% 落库失败。
修复：与 _upsert_decision:1316/1317 一致，空→None(SQL NULL)。
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.persistence.erp_writer.repository import ErpDualWriterRepository


def _call(p3: dict):
    repo = ErpDualWriterRepository.__new__(ErpDualWriterRepository)  # 跳过 __init__，不连库
    cur = MagicMock()
    run = SimpleNamespace(decision_id="dec_test")
    repo._upsert_ai_suggest(cur, run, {"p3": p3}, datetime.now())
    cur.execute.assert_called_once()
    return cur.execute.call_args[0][1]   # params 元组


def test_empty_budget_and_acos_become_null():
    # budget_realloc 兜底：budget_bid 无 suggested、target_acos 无 recommended_target
    params = _call({"target_acos": {}, "budget_bid": {}})
    assert params[2] is None   # suggest_acos（int 列）：空→NULL，不再 ''→0 脏数据
    assert params[7] is None   # suggest_budget（decimal 列）：空→NULL，不再 ''→1366 崩库


def test_none_values_become_null():
    params = _call({"target_acos": {"recommended_target": None},
                    "budget_bid": {"suggested": None}})
    assert params[2] is None
    assert params[7] is None


def test_real_values_passed_through():
    params = _call({"target_acos": {"recommended_target": 25},
                    "budget_bid": {"suggested": 12.5}})
    assert params[2] == "25"     # 有值 → 字符串传入，MySQL 按列转 int/decimal
    assert params[7] == "12.5"
