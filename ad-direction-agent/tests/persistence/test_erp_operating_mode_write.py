"""ERP 经营模式列的 SQL 写入契约。"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.persistence.erp_writer.repository import ErpDualWriterRepository


class _Cursor:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple):
        self.calls.append((sql, params))


def _repo() -> ErpDualWriterRepository:
    return ErpDualWriterRepository(
        host="localhost", port=3306, user="test", password="", database="test"
    )


def _run():
    return SimpleNamespace(
        decision_id="dec_test",
        parent_asin="B0TEST",
        parent_seller_sku="SKU-1",
        shop_id=1622,
        site_code="Amazon_US",
        batch_no="batch-1",
    )


def test_decision_and_config_write_nullable_operating_mode_as_erp_code():
    repo = _repo()
    repo._audit_int_val = None
    run = _run()
    meta = {
        "product_position": "重点产品 (P1)",
        "product_stage": "推进期",
        "season_type": "旺季准备",
        "operating_mode": "控制清货",
    }
    now = datetime(2026, 7, 25)

    decision_cursor = _Cursor()
    repo._upsert_decision(decision_cursor, run, meta, now)
    config_cursor = _Cursor()
    repo._upsert_decision_config(config_cursor, run, meta, now)

    for sql, params in (*decision_cursor.calls, *config_cursor.calls):
        assert "operating_mode" in sql
        assert "CONTROLLED_CLEARANCE" in params
        assert sql.count("%s") == len(params)
