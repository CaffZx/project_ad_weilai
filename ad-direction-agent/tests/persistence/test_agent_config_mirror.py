"""t_advet_agent_config 的字段级镜像写入契约。"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.persistence.erp_writer.repository import ErpDualWriterRepository


class _Cursor:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params: tuple):
        self.calls.append((sql, params))


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
    assert "INSERT INTO t_advet_agent_config" in sql
    assert "product_position=VALUES(product_position)" in sql
    assert "operating_mode=VALUES(operating_mode)" in sql
    assert "season_type=VALUES(season_type)" in sql
    assert "advert_purposes=VALUES(advert_purposes)" not in sql
    assert "P1_PRODUCT" in params
    assert "CONTROLLED_CLEARANCE" in params
    assert "PEAK_SEASON_PREPARE" in params
    assert conn.committed is True
    assert conn.closed is True


def test_upsert_agent_config_preserves_unpatched_columns_and_supports_explicit_null():
    repo, conn = _repo()

    repo.upsert_agent_config(
        identity=_identity(user_id="operator"),
        patch={"target_acos_suggest": None},
    )

    sql, params = conn.cursor_obj.calls[0]
    assert "target_acos_suggest=VALUES(target_acos_suggest)" in sql
    assert "daily_budget_suggest=VALUES(daily_budget_suggest)" not in sql
    assert None in params
    assert conn.committed is True


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
