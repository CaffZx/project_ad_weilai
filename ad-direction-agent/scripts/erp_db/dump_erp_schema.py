"""Dump live ERP schema for t_advert_agent* tables."""
from __future__ import annotations

import sys

import pymysql

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
from _erp_conn import ERP_DEFAULT, erp_pymysql_conn


def main() -> None:
    conn = pymysql.connect(**erp_pymysql_conn(read_timeout=60))
    cur = conn.cursor()
    db = ERP_DEFAULT["database"]

    cur.execute(
        """
        SELECT TABLE_NAME, TABLE_COMMENT, TABLE_ROWS, CREATE_TIME, UPDATE_TIME
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME LIKE 't_advert_agent%%'
        ORDER BY TABLE_NAME
        """,
        (db,),
    )
    tables = cur.fetchall()
    print("=== TABLES (t_advert_agent*) ===")
    for t in tables:
        rows = t["TABLE_ROWS"] or 0
        comment = t["TABLE_COMMENT"] or ""
        print(f"  {t['TABLE_NAME']:52s} rows~{rows:>5}  {comment}")

    for t in tables:
        name = t["TABLE_NAME"]
        print("\n" + "=" * 76)
        print(f"TABLE: {name}")
        if t.get("TABLE_COMMENT"):
            print(f"Table comment: {t['TABLE_COMMENT']}")

        cur.execute(
            """
            SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_DEFAULT,
                   EXTRA, COLUMN_COMMENT
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
            ORDER BY ORDINAL_POSITION
            """,
            (db, name),
        )
        print(f"{'Column':30s} {'Type':24s} Null Key Default     Comment")
        print("-" * 76)
        for c in cur.fetchall():
            default = c["COLUMN_DEFAULT"]
            if default is not None and len(str(default)) > 12:
                default = str(default)[:12] + "..."
            print(
                f"{c['COLUMN_NAME']:30s} {c['COLUMN_TYPE']:24s} "
                f"{c['IS_NULLABLE']:4s} {(c['COLUMN_KEY'] or ''):3s} "
                f"{str(default or ''):12s} {c['COLUMN_COMMENT'] or ''}"
            )

        cur.execute(
            """
            SELECT INDEX_NAME, NON_UNIQUE,
                   GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS cols
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s
            GROUP BY INDEX_NAME, NON_UNIQUE
            ORDER BY INDEX_NAME
            """,
            (db, name),
        )
        idxs = cur.fetchall()
        if idxs:
            print("Indexes:")
            for i in idxs:
                kind = "UNIQUE" if i["NON_UNIQUE"] == 0 else "INDEX"
                print(f"  [{kind}] {i['INDEX_NAME']}: ({i['cols']})")

    conn.close()


if __name__ == "__main__":
    main()
