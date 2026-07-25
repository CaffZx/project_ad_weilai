"""State 建表只负责新库，运行时不得自行迁移列。"""
from __future__ import annotations

from app.persistence.mysql_state_manager import MySQLStateManager


class _Cursor:
    def __init__(self):
        self.sql: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql: str, *_params):
        self.sql.append(sql)

    def fetchone(self):
        return {"cnt": 1}

    def fetchall(self):
        return [{"COLUMN_NAME": "shop_id"}, {"COLUMN_NAME": "parent_seller_sku"}]


class _Conn:
    def __init__(self):
        self.cursor_obj = _Cursor()

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        return None

    def close(self):
        return None


def test_ensure_schema_never_checks_or_alters_existing_columns(monkeypatch):
    conn = _Conn()
    monkeypatch.setattr(
        "app.persistence.mysql_state_manager.pymysql.connect", lambda **_kwargs: conn
    )

    MySQLStateManager().ensure_schema()

    executed = "\n".join(conn.cursor_obj.sql).upper()
    assert "INFORMATION_SCHEMA.COLUMNS" not in executed
    assert "ALTER TABLE" not in executed
