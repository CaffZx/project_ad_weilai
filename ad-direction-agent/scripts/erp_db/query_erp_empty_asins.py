"""Query ERP summary/card counts for given parent ASINs."""
from __future__ import annotations

import pymysql

from _erp_conn import erp_pymysql_conn


def main() -> None:
    conn = pymysql.connect(**erp_pymysql_conn())
    cur = conn.cursor()
    for asin in ("B0D8JDVVPT", "B0DDWX58G3", "B0B7S3PWWB"):
        cur.execute(
            """
            SELECT parent_asin, decision_id, total_count, eliminate_count, adjust_count, keep_count
            FROM t_advert_agent_modify_suggest_summary
            WHERE parent_asin = %s
            ORDER BY update_time DESC LIMIT 2
            """,
            (asin,),
        )
        print("summary", asin, cur.fetchall())
        cur.execute(
            """
            SELECT parent_asin, decision_id, COUNT(1) AS n
            FROM t_advert_agent_modify_suggest_card
            WHERE parent_asin = %s
            GROUP BY parent_asin, decision_id
            """,
            (asin,),
        )
        print("cards", asin, cur.fetchall())
    conn.close()


if __name__ == "__main__":
    main()
