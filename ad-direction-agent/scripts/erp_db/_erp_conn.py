"""Default ERP MySQL connection (override via CLI where supported).

凭据从环境变量读取（ERP_HOST/ERP_PORT/ERP_USER/ERP_PASSWORD/ERP_DATABASE），
勿在源码硬编码密码。运行前先 `export ERP_PASSWORD=...` 或经 .env 注入。
"""
from __future__ import annotations

import os

from pymysql.cursors import DictCursor

ERP_DEFAULT = {
    "host": os.getenv("ERP_HOST", "192.168.2.51"),
    "port": int(os.getenv("ERP_PORT", "3306")),
    "user": os.getenv("ERP_USER", "erp_agentadvert"),
    "password": os.getenv("ERP_PASSWORD", ""),
    "database": os.getenv("ERP_DATABASE", "erp_agentadvert"),
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
