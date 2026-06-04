"""Verify full ERP write coverage for a decision_id."""
from __future__ import annotations

import argparse
import sys

import pymysql

from _erp_conn import erp_pymysql_conn


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--decision-id", required=True)
    parser.add_argument("--expected-cards", type=int, default=None)
    args = parser.parse_args()
    did = args.decision_id

    conn = pymysql.connect(**erp_pymysql_conn())
    cur = conn.cursor()
    issues: list[str] = []

    checks = [
        ("t_advert_agent_decision", "SELECT COUNT(1) n FROM t_advert_agent_decision WHERE id=%s", (did,), 1),
        ("t_advert_agent_decision_config", "SELECT COUNT(1) n FROM t_advert_agent_decision_config WHERE parent_asin=(SELECT parent_asin FROM t_advert_agent_decision WHERE id=%s)", (did,), 1),
        ("t_advert_agent_modify_suggest_summary", "SELECT COUNT(1) n FROM t_advert_agent_modify_suggest_summary WHERE decision_id=%s", (did,), 1),
        ("t_advert_agent_modify_suggest_card", "SELECT COUNT(1) n FROM t_advert_agent_modify_suggest_card WHERE decision_id=%s", (did,), None),
        ("t_advert_agent_purpose_score", "SELECT COUNT(1) n FROM t_advert_agent_purpose_score WHERE decision_id=%s", (did,), 1),
        ("t_advert_agent_core_keyword_tracking", "SELECT COUNT(1) n FROM t_advert_agent_core_keyword_tracking WHERE decision_id=%s", (did,), 1),
        ("t_advert_agent_ai_suggest", "SELECT COUNT(1) n FROM t_advert_agent_ai_suggest WHERE decision_id=%s", (did,), 1),
        ("t_advert_agent_direction_recommend", "SELECT COUNT(1) n FROM t_advert_agent_direction_recommend WHERE decision_id=%s", (did,), 1),
        ("t_advert_agent_direction_recommend_detail", "SELECT COUNT(1) n FROM t_advert_agent_direction_recommend_detail WHERE decision_id=%s", (did,), 4),
    ]

    print(f"=== verify decision_id={did} ===")
    for name, sql, params, min_n in checks:
        cur.execute(sql, params)
        n = cur.fetchone()["n"]
        ok = n >= (min_n or 0)
        if min_n is not None and n < min_n:
            issues.append(f"{name}: {n} < {min_n}")
            ok = False
        print(f"  {'OK' if ok else 'FAIL'} {name}: {n}")

    if args.expected_cards is not None:
        cur.execute(
            "SELECT COUNT(1) n FROM t_advert_agent_modify_suggest_card WHERE decision_id=%s",
            (did,),
        )
        cards = cur.fetchone()["n"]
        if cards != args.expected_cards:
            issues.append(f"cards {cards} != expected {args.expected_cards}")
        print(f"  cards expected={args.expected_cards} actual={cards}")

    cur.execute(
        """
        SELECT main_push_count, broad_auto_count, test_new_count, eliminate_bubble_count
        FROM t_advert_agent_modify_suggest_summary WHERE decision_id=%s
        """,
        (did,),
    )
    row = cur.fetchone()
    if row:
        print(f"  budget_groups: {row}")

    conn.close()
    if issues:
        print("VERDICT: FAIL")
        for i in issues:
            print(f"  - {i}")
        sys.exit(1)
    print("VERDICT: PASS")


if __name__ == "__main__":
    main()
