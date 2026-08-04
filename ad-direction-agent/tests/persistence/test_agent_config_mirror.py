"""t_advert_agent_config 的字段级镜像写入契约。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.persistence.erp_writer.repository import ErpDualWriterRepository


class _Cursor:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params: tuple):
        self.calls.append((sql, params))

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self):
        self.cursor_obj = _Cursor()
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _repo() -> tuple[ErpDualWriterRepository, _Connection]:
    repo = ErpDualWriterRepository(
        host="localhost", port=3306, user="test", password="", database="test"
    )
    conn = _Connection()
    repo._connect = lambda: conn  # type: ignore[method-assign]
    return repo, conn


def _identity(**overrides):
    identity = {
        "parent_asin": "B0TEST",
        "parent_seller_sku": "SKU-1",
        "shop_id": 1622,
        "shop_account": "demo-shop",
        "site_code": "US",
        "user_id": "42",
        "day_range": "DAY_7",
    }
    identity.update(overrides)
    return identity


def test_upsert_agent_config_maps_strategy_patch_and_updates_only_patch_columns():
    repo, conn = _repo()

    repo.upsert_agent_config(
        identity=_identity(),
        patch={
            "product_position": "重点产品 (P1)",
            "operating_mode": "控制清货",
            "season_type": "旺季准备",
        },
    )

    sql, params = conn.cursor_obj.calls[0]
    assert "INSERT INTO t_advert_agent_config" in sql
    assert "product_position=VALUES(product_position)" in sql
    assert "operating_mode=VALUES(operating_mode)" in sql
    assert "season_type=VALUES(season_type)" in sql
    assert "advert_purposes=VALUES(advert_purposes)" not in sql
    assert "P1_PRODUCT" in params
    assert "CONTROLLED_CLEARANCE" in params
    assert "PEAK_SEASON_PREPARE" in params
    assert conn.committed is True
    assert conn.closed is True


def test_upsert_agent_config_preserves_unpatched_columns_and_skips_empty_values():
    repo, conn = _repo()

    repo.upsert_agent_config(
        identity=_identity(user_id="operator"),
        patch={"target_acos_suggest": None},
    )

    sql, params = conn.cursor_obj.calls[0]
    assert "target_acos_suggest=VALUES(target_acos_suggest)" not in sql
    assert "daily_budget_suggest=VALUES(daily_budget_suggest)" not in sql
    # 业务列区(base 6 列 + mapped patch)无 None;audit 列(user_id 非数字 → None)不属于业务列
    assert None not in params[:6]
    assert conn.committed is True


def test_upsert_agent_config_skips_empty_list_columns_without_blocking_others():
    repo, conn = _repo()

    repo.upsert_agent_config(
        identity=_identity(),
        patch={
            "advert_purposes": [],
            "advert_direction_types": [],
            "target_acos_suggest": 25,
        },
    )

    sql, params = conn.cursor_obj.calls[0]
    assert "advert_purposes=VALUES(advert_purposes)" not in sql
    assert "advert_direction_types=VALUES(advert_direction_types)" not in sql
    assert "target_acos_suggest=VALUES(target_acos_suggest)" in sql
    assert None not in params[:7]


def test_upsert_agent_config_maps_list_and_decimal_fields():
    repo, conn = _repo()

    repo.upsert_agent_config(
        identity=_identity(),
        patch={
            "advert_purposes": ["转化型", "排名型"],
            "target_keyword_types": ["长尾词"],
            "advert_direction_types": ["push_natural", "optimize_acos"],
            "daily_budget_suggest": Decimal("12.50"),
        },
    )

    _, params = conn.cursor_obj.calls[0]
    assert '["CONVERSION", "RANKING"]' in params
    assert '["LONG_TAIL"]' in params
    assert '["PUSH_NATURAL", "OPTIMIZE_ACOS"]' in params
    assert Decimal("12.50") in params


def test_upsert_agent_config_rejects_incomplete_identity_before_connecting():
    repo, conn = _repo()

    with pytest.raises(ValueError, match="identity is incomplete"):
        repo.upsert_agent_config(
            identity=_identity(parent_seller_sku=""),
            patch={"target_acos_suggest": 25},
        )

    assert conn.cursor_obj.calls == []
    assert conn.closed is False


def test_get_agent_config_row_reads_by_unique_identity_without_site_filter():
    repo, conn = _repo()
    conn.cursor_obj.row = {"product_position": "P2_PRODUCT"}

    row = repo.get_agent_config_row(_identity(site_code="Amazon_US"))

    sql, params = conn.cursor_obj.calls[0]
    assert "FROM t_advert_agent_config" in sql
    assert "parent_asin=%s AND parent_seller_sku=%s AND shop_id=%s" in sql
    assert "site_code=%s" not in sql
    assert params == ("B0TEST", "SKU-1", 1622)
    assert row == {"product_position": "P2_PRODUCT"}
    assert conn.closed is True
