"""Verify t_advert_agent_modify_suggest_card keyword / keyword_match_type semantics."""
from __future__ import annotations

import pymysql

from _erp_conn import ERP_DEFAULT, erp_pymysql_conn
MATCH = ("EXACT", "PHRASE", "BROAD", "NEGATIVE", "NEG")


def main() -> None:
    conn = pymysql.connect(**erp_pymysql_conn(read_timeout=60))
    cur = conn.cursor()

    cur.execute(
        """
        SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=%s AND TABLE_NAME='t_advert_agent_modify_suggest_card'
          AND COLUMN_NAME IN ('keyword','keyword_match_type')
        ORDER BY ORDINAL_POSITION
        """,
        (ERP_DEFAULT["database"],),
    )
    print("=== COLUMN DEFINITION ===")
    for r in cur.fetchall():
        print(f"  {r['COLUMN_NAME']:22s} {r['COLUMN_TYPE']:20s} comment={r['COLUMN_COMMENT']!r}")

    cur.execute("SELECT COUNT(1) AS n FROM t_advert_agent_modify_suggest_card")
    total = cur.fetchone()["n"]
    print(f"\n=== CARD ROWS: {total} ===")

    cur.execute(
        """
        SELECT COUNT(1) AS n FROM t_advert_agent_modify_suggest_card
        WHERE keyword IN %s AND (keyword_match_type IS NULL OR keyword_match_type NOT IN %s)
        """,
        (MATCH, MATCH),
    )
    wrong_kw_col = cur.fetchone()["n"]

    cur.execute(
        """
        SELECT COUNT(1) AS n FROM t_advert_agent_modify_suggest_card
        WHERE keyword_match_type IN %s
        """,
        (MATCH,),
    )
    correct_mt_col = cur.fetchone()["n"]

    cur.execute(
        """
        SELECT COUNT(1) AS n FROM t_advert_agent_modify_suggest_card
        WHERE keyword IN %s AND keyword_match_type IN %s
        """,
        (MATCH, MATCH),
    )
    both_match = cur.fetchone()["n"]

    print("Heuristic (match type enum in column):")
    print(f"  keyword IN match_types (OLD/wrong write):        {wrong_kw_col}")
    print(f"  keyword_match_type IN match_types (expected):    {correct_mt_col}")
    print(f"  both columns are match types:                    {both_match}")

    cur.execute(
        """
        SELECT COUNT(1) AS n
        FROM t_advert_agent_modify_suggest_card c
        JOIN t_advert_agent_modify_keyword_pending k ON k.suggest_card_id = c.id
        WHERE k.match_type IS NOT NULL AND k.match_type != ''
          AND c.keyword_match_type = k.match_type
          AND (c.keyword = k.keyword_text OR (c.keyword IS NULL AND k.keyword_text IS NULL))
        """
    )
    aligned = cur.fetchone()["n"]

    cur.execute(
        """
        SELECT COUNT(1) AS n
        FROM t_advert_agent_modify_suggest_card c
        JOIN t_advert_agent_modify_keyword_pending k ON k.suggest_card_id = c.id
        WHERE k.match_type IS NOT NULL AND k.match_type != ''
          AND c.keyword IN %s
        """,
        (MATCH,),
    )
    misaligned = cur.fetchone()["n"]

    cur.execute(
        """
        SELECT COUNT(1) AS n
        FROM t_advert_agent_modify_suggest_card c
        JOIN t_advert_agent_modify_keyword_pending k ON k.suggest_card_id = c.id
        """
    )
    join_total = cur.fetchone()["n"]

    print(f"\nCross-check vs keyword_pending (joined {join_total}):")
    print(f"  aligned (card matches pending):   {aligned}")
    print(f"  misaligned (keyword=match enum):  {misaligned}")

    if misaligned == 0 and wrong_kw_col == 0 and correct_mt_col > 0:
        verdict = "PASS — card columns match pending semantics"
    elif wrong_kw_col > 0 or misaligned > 0:
        verdict = "FAIL — data still uses old swapped mapping; re-run write or SQL fix"
    else:
        verdict = "INCONCLUSIVE — review samples below"
    print(f"\n=== VERDICT: {verdict} ===")

    print("\n=== SAMPLES (10 recent rows with keyword data) ===")
    cur.execute(
        """
        SELECT parent_asin, LEFT(campaign_name, 50) AS campaign_name,
               LEFT(keyword, 50) AS keyword,
               LEFT(keyword_match_type, 20) AS keyword_match_type,
               update_time
        FROM t_advert_agent_modify_suggest_card
        WHERE (keyword IS NOT NULL AND keyword != '')
           OR (keyword_match_type IS NOT NULL AND keyword_match_type != '')
        ORDER BY update_time DESC
        LIMIT 10
        """
    )
    for i, r in enumerate(cur.fetchall(), 1):
        print(
            f"{i}. {r['parent_asin']} | keyword={r['keyword']!r} | "
            f"keyword_match_type={r['keyword_match_type']!r}"
        )

    conn.close()


if __name__ == "__main__":
    main()
