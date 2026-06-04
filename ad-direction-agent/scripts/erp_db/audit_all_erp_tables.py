"""Audit all 18 t_advert_agent_* tables — row counts and per-decision breakdown."""
from __future__ import annotations

import pymysql

from _erp_conn import erp_pymysql_conn

DIDS = {
    "B0B7S3PWWB": "dec555e98e7cf27ac40ba2f2cd3209b9",
    "B0CGH9QRKK": "dec5da0a62fe024fa952815cd44be083",
}

ALL_TABLES = [
    "t_advert_agent_decision",
    "t_advert_agent_decision_config",
    "t_advert_agent_modify_suggest_summary",
    "t_advert_agent_modify_suggest_card",
    "t_advert_agent_modify_keyword_pending",
    "t_advert_agent_modify_campaign_pending",
    "t_advert_agent_modify_placement_pending",
    "t_advert_agent_data_metrics",
    "t_advert_agent_direction_recommend",
    "t_advert_agent_direction_recommend_detail",
    "t_advert_agent_purpose_score",
    "t_advert_agent_core_keyword_tracking",
    "t_advert_agent_ai_suggest",
    "t_advert_agent_modify_advert_record",
    "t_advert_agent_modify_keyword_record",
    "t_advert_agent_modify_campaign_record",
    "t_advert_agent_modify_placement_record",
    "t_advert_agent_modify_portfolio_record",
]

RECORD_TABLES = {
    "t_advert_agent_modify_advert_record",
    "t_advert_agent_modify_keyword_record",
    "t_advert_agent_modify_campaign_record",
    "t_advert_agent_modify_placement_record",
    "t_advert_agent_modify_portfolio_record",
}


def _count_by_decision(cur, table: str, decision_id: str) -> int:
    if table == "t_advert_agent_decision_config":
        cur.execute(
            "SELECT COUNT(1) n FROM t_advert_agent_decision_config "
            "WHERE parent_asin=(SELECT parent_asin FROM t_advert_agent_decision WHERE id=%s)",
            (decision_id,),
        )
    else:
        cur.execute(f"SELECT COUNT(1) n FROM `{table}` WHERE decision_id=%s", (decision_id,))
    return int(cur.fetchone()["n"])


def main() -> int:
    conn = pymysql.connect(**erp_pymysql_conn())
    cur = conn.cursor()
    issues: list[str] = []

    print("=" * 72)
    print("ALL 18 TABLES — TOTAL ROWS (erp_agentadvert @ 192.168.2.51)")
    print("=" * 72)
    grand = 0
    for table in ALL_TABLES:
        cur.execute(f"SELECT COUNT(1) n FROM `{table}`")
        n = cur.fetchone()["n"]
        grand += n
        tag = "WHP执行回写-预期0" if table in RECORD_TABLES else ""
        print(f"  {table:52s} {n:>6d}  {tag}")
    print(f"  {'GRAND TOTAL':52s} {grand:>6d}")

    print("\n" + "=" * 72)
    print("PER DECISION BREAKDOWN")
    print("=" * 72)
    for asin, did in DIDS.items():
        print(f"\n--- {asin} | {did} ---")
        cur.execute("SELECT COUNT(1) n FROM t_advert_agent_decision WHERE id=%s", (did,))
        if cur.fetchone()["n"] != 1:
            issues.append(f"{asin}: decision row missing")
        for table in ALL_TABLES:
            if table in ("t_advert_agent_decision", "t_advert_agent_decision_config"):
                continue
            if table in RECORD_TABLES:
                continue
            n = _count_by_decision(cur, table, did)
            print(f"  {table:48s} {n:>4d}")

    print("\n" + "=" * 72)
    print("INTEGRITY SPOT CHECKS")
    print("=" * 72)
    cur.execute(
        "SELECT decision_id, campaign_id, COUNT(1) n "
        "FROM t_advert_agent_modify_suggest_card GROUP BY 1,2 HAVING n > 1"
    )
    dup = cur.fetchall()
    print(f"  card UK duplicates: {len(dup)}")
    if dup:
        issues.append(f"card UK duplicates: {len(dup)}")

    for label, ptable in [
        ("keyword_pending", "t_advert_agent_modify_keyword_pending"),
        ("campaign_pending", "t_advert_agent_modify_campaign_pending"),
        ("placement_pending", "t_advert_agent_modify_placement_pending"),
    ]:
        cur.execute(
            f"SELECT COUNT(1) n FROM {ptable} p WHERE NOT EXISTS "
            f"(SELECT 1 FROM t_advert_agent_modify_suggest_card c WHERE c.id=p.suggest_card_id)"
        )
        orphans = cur.fetchone()["n"]
        print(f"  orphan {label}: {orphans}")
        if orphans:
            issues.append(f"orphan {label}: {orphans}")

    cur.execute(
        "SELECT SUM(keyword_id LIKE 'kwd%%') n FROM t_advert_agent_modify_keyword_pending"
    )
    fake = int(cur.fetchone()["n"] or 0)
    print(f"  fake kwd%% keyword_id: {fake}")
    if fake:
        issues.append(f"fake keyword_id: {fake}")

    for asin, did in DIDS.items():
        cur.execute(
            "SELECT total_count FROM t_advert_agent_modify_suggest_summary WHERE decision_id=%s",
            (did,),
        )
        tc = cur.fetchone()["total_count"]
        cur.execute(
            "SELECT COUNT(1) n FROM t_advert_agent_modify_suggest_card WHERE decision_id=%s",
            (did,),
        )
        cards = cur.fetchone()["n"]
        exp = 105 if "B0B7" in asin else 56
        ok = cards == exp
        print(f"  {asin} cards={cards} (expect {exp}) summary.total={tc}  {'OK' if ok else 'FAIL'}")
        if not ok:
            issues.append(f"{asin}: cards {cards} != {exp}")

    print("\n" + "=" * 72)
    print("VERDICT")
    print("=" * 72)
    if issues:
        for i in issues:
            print(f"  FAIL: {i}")
        conn.close()
        return 1
    print("  ALL TABLE CHECKS PASSED")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
