"""override 持久化回归测试（交接文档 §21，2026-06-22 上线 prod）。

锁住"acos / 预算 override 不再按时间过期"这一核心语义：
即便 DB 行的 expires_at 已是过去时间，读取仍须透出该值（持久继承），
失效途径仅「取消覆盖」或「保存新值覆盖」。

此前 MySQLStateManager（646 行）零测试覆盖。本测试全程离线——
mock 掉 ensure_schema + _execute，绝不连真实 state 库。
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from app.persistence.mysql_state_manager import MySQLStateManager


PAST = datetime.now(timezone.utc) - timedelta(days=30)  # 早已过期的 expires_at


@pytest.fixture
def mgr():
    """无参构造（仅建 config 目录 + 锁，不连库）；屏蔽 schema 连库。"""
    m = MySQLStateManager()
    m.ensure_schema = lambda: None  # type: ignore[method-assign]
    return m


# ── acos override 持久化 ──────────────────────────────────────

def test_acos_override_returns_value_even_when_expired(mgr):
    # 核心：expires_at 已过期，仍须返回该值（不再按时间过期）
    mgr._execute = Mock(return_value={"value": 35, "expires_at": PAST})
    assert mgr.get_target_acos_override("B0X") == 35


def test_acos_override_returns_int(mgr):
    mgr._execute = Mock(return_value={"value": "30", "expires_at": PAST})
    result = mgr.get_target_acos_override("B0X")
    assert result == 30 and isinstance(result, int)


def test_acos_override_none_when_no_row(mgr):
    # 行不存在（运营从未设/已取消覆盖）→ None
    mgr._execute = Mock(return_value=None)
    assert mgr.get_target_acos_override("B0X") is None


# ── 预算 override 持久化（get_long_term_config 第 3 个 _execute）──

def _ltc_rows(bud_row):
    """get_long_term_config 内 _execute 调用顺序：strategy / tactics / budget。"""
    return [None, None, bud_row]


def test_budget_override_surfaces_even_when_expired(mgr):
    mgr._execute = Mock(side_effect=_ltc_rows({"value": 42.0, "expires_at": PAST}))
    data = mgr.get_long_term_config("B0X")
    assert data["daily_budget_override"] == 42.0


def test_budget_override_absent_when_no_row(mgr):
    mgr._execute = Mock(side_effect=_ltc_rows(None))
    data = mgr.get_long_term_config("B0X")
    assert "daily_budget_override" not in data
