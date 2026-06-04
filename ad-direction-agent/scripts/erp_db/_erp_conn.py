"""Default ERP MySQL connection (override via CLI where supported)."""
from __future__ import annotations

from pymysql.cursors import DictCursor

ERP_DEFAULT = {
    "host": "192.168.2.51",
    "port": 3306,
    "user": "erp_agentadvert",
    "password": "erp_agentadvert#weilai123",
    "database": "erp_agentadvert",
}


def erp_pymysql_conn(
    *,
    read_timeout: int | None = None,
    write_timeout: int | None = None,
    connect_timeout: int = 15,
    **extra,
) -> dict:
    """Kwargs for pymysql.connect targeting erp_agentadvert."""
    kw: dict = {
        "host": ERP_DEFAULT["host"],
        "port": ERP_DEFAULT["port"],
        "user": ERP_DEFAULT["user"],
        "password": ERP_DEFAULT["password"],
        "database": ERP_DEFAULT["database"],
        "charset": "utf8mb4",
        "cursorclass": DictCursor,
        "connect_timeout": connect_timeout,
    }
    if read_timeout is not None:
        kw["read_timeout"] = read_timeout
    if write_timeout is not None:
        kw["write_timeout"] = write_timeout
    kw.update(extra)
    return kw
