"""ERP database structural health check — referential integrity, field validity, summary consistency."""
from __future__ import annotations

import pymysql

from _erp_conn import erp_pymysql_conn

TABLES = [
    "t_advert_agent_modify_suggest_summary",
    "t_advert_agent_modify_suggest_card",
    "t_advert_agent_modify_keyword_pending",
    "t_advert_agent_modify_campaign_pending",
    "t_advert_agent_modify_placement_pending",
    "t_advert_agent_direction_recommend",
    "t_advert_agent_direction_recommend_detail",
    "t_advert_agent_data_metrics",
]


def main():
    conn = pymysql.connect(**erp_pymysql_conn(connect_timeout=10, read_timeout=60))
    cur = conn.cursor()
    issues: list[str] = []
    ok: list[str] = []

    # ── 1. row counts ──
    print("=" * 60)
    print("TABLE ROW COUNTS")
    print("=" * 60)
    for t in TABLES:
        try:
            cur.execute(f"SELECT COUNT(1) AS n FROM {t}")
            n = cur.fetchone()["n"]
            print(f"  {t:52s} {n:>6d}")
        except Exception as e:
            issues.append(f"TABLE MISSING: {t}")
            print(f"  {t:52s} ERROR: {e}")

    # ── 2. decision_id coverage ──
    print("\n" + "=" * 60)
    print("DECISION_ID COVERAGE (per decision_id)")
    print("=" * 60)
    cur.execute("SELECT DISTINCT parent_asin, decision_id, total_count FROM t_advert_agent_modify_suggest_summary ORDER BY parent_asin, decision_id")
    summaries = cur.fetchall()
    print(f"  total decisions: {len(summaries)}")
    for s in summaries:
        did = s["decision_id"]; asin = s["parent_asin"]; tc = s.get("total_count", 0)
        cur.execute("SELECT COUNT(1) AS n FROM t_advert_agent_modify_suggest_card WHERE decision_id=%s", (did,))
        cards = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(1) AS n FROM t_advert_agent_modify_keyword_pending WHERE decision_id=%s", (did,))
        kw = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(1) AS n FROM t_advert_agent_modify_campaign_pending WHERE decision_id=%s", (did,))
        camp = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(1) AS n FROM t_advert_agent_modify_placement_pending WHERE decision_id=%s", (did,))
        pl = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(1) AS n FROM t_advert_agent_direction_recommend WHERE decision_id=%s", (did,))
        leg_rec = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(1) AS n FROM t_advert_agent_direction_recommend_detail WHERE decision_id=%s", (did,))
        leg_det = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(1) AS n FROM t_advert_agent_data_metrics WHERE decision_id=%s", (did,))
        metrics = cur.fetchone()["n"]
        status = "✓" if cards > 0 else "✗ EMPTY"
        print(f"  {asin:20s} | {did[:14]} | summary:{tc:>3d} cards:{cards:>3d} kw:{kw:>3d} camp:{camp:>3d} pl:{pl:>3d} rec:{leg_rec} det:{leg_det} met:{metrics}  {status}")

    # ── 3. orphan checks ──
    print("\n" + "=" * 60)
    print("ORPHAN CHECKS (pending FK → card / detail FK → recommend)")
    print("=" * 60)
    for label, ptable, fk_col, ref_table, ref_col in [
        ("keyword_pending", "t_advert_agent_modify_keyword_pending", "suggest_card_id",
         "t_advert_agent_modify_suggest_card", "id"),
        ("campaign_pending", "t_advert_agent_modify_campaign_pending", "suggest_card_id",
         "t_advert_agent_modify_suggest_card", "id"),
        ("placement_pending", "t_advert_agent_modify_placement_pending", "suggest_card_id",
         "t_advert_agent_modify_suggest_card", "id"),
        ("legacy_detail", "t_advert_agent_direction_recommend_detail", "direction_recommend_id",
         "t_advert_agent_direction_recommend", "id"),
    ]:
        cur.execute(f"SELECT COUNT(1) AS n FROM {ptable} p WHERE NOT EXISTS (SELECT 1 FROM {ref_table} r WHERE r.{ref_col} = p.{fk_col})")
        n = cur.fetchone()["n"]
        if n > 0:
            issues.append(f"ORPHAN {label}: {n} rows")
            print(f"  {label}: {n} ORPHANS ✗")
        else:
            ok.append(f"no orphans in {label}")
            print(f"  {label}: 0 ✓")

    # ── 4. suggest_card NULL FK ──
    print("\n" + "=" * 60)
    print("NULL FK IN PENDING TABLES")
    print("=" * 60)
    for label, ptable in [
        ("keyword_pending", "t_advert_agent_modify_keyword_pending"),
        ("campaign_pending", "t_advert_agent_modify_campaign_pending"),
        ("placement_pending", "t_advert_agent_modify_placement_pending"),
    ]:
        cur.execute(f"SELECT COUNT(1) AS n FROM {ptable} WHERE suggest_card_id IS NULL")
        n = cur.fetchone()["n"]
        if n > 0:
            issues.append(f"NULL FK in {ptable}: {n} rows")
            print(f"  {label}: {n} rows with NULL suggest_card_id ✗")
        else:
            print(f"  {label}: all have FK ✓")

    # ── 5. enum validity ──
    print("\n" + "=" * 60)
    print("ENUM: placement_type")
    print("=" * 60)
    VALID_PL = {"TOP_OF_SEARCH", "REST_OF_SEARCH", "PRODUCT_PAGE"}
    cur.execute("SELECT placement_type, COUNT(1) AS n FROM t_advert_agent_modify_placement_pending GROUP BY placement_type")
    for r in cur.fetchall():
        label = "✓" if r["placement_type"] in VALID_PL else "✗ INVALID"
        print(f"  {r['placement_type']:30s} {r['n']:>5d}  {label}")
        if r["placement_type"] not in VALID_PL:
            issues.append(f"INVALID placement_type: {r['placement_type']} ({r['n']} rows)")
    cur.execute("SELECT COUNT(1) AS n FROM t_advert_agent_modify_placement_pending WHERE placement_type IS NULL OR placement_type=''")
    n_null = cur.fetchone()["n"]
    if n_null:
        issues.append(f"NULL/EMPTY placement_type: {n_null} rows")
    print(f"  null/empty: {n_null}")

    print("\n" + "=" * 60)
    print("ENUM: suggest_category & confidence_level")
    print("=" * 60)
    VALID_CAT = {"ELIMINATE", "PAUSED", "REACTIVATE", "ADJUST", "KEEP", "CREATE"}
    VALID_CONF = {"high", "medium", "low"}
    cur.execute("SELECT suggest_category, COUNT(1) AS n FROM t_advert_agent_modify_suggest_card GROUP BY suggest_category")
    for r in cur.fetchall():
        label = "✓" if r["suggest_category"] in VALID_CAT else "✗ INVALID"
        print(f"  category {r['suggest_category']:15s} {r['n']:>5d}  {label}")
        if r["suggest_category"] not in VALID_CAT:
            issues.append(f"INVALID suggest_category: {r['suggest_category']}")
    cur.execute("SELECT confidence_level, COUNT(1) AS n FROM t_advert_agent_modify_suggest_card GROUP BY confidence_level")
    for r in cur.fetchall():
        label = "✓" if r["confidence_level"] in VALID_CONF else "✗ INVALID"
        print(f"  conf     {r['confidence_level']:15s} {r['n']:>5d}  {label}")
        if r["confidence_level"] not in VALID_CONF:
            issues.append(f"INVALID confidence_level: {r['confidence_level']}")

    print("\n" + "=" * 60)
    print("ENUM: direction_recommend_detail.direction_type (informational)")
    print("=" * 60)
    cur.execute("SELECT direction_type, COUNT(1) AS n FROM t_advert_agent_direction_recommend_detail GROUP BY direction_type ORDER BY n DESC")
    for r in cur.fetchall():
        print(f"  {r['direction_type']:30s} {r['n']:>5d}")

    # ── 6. summary consistency ──
    print("\n" + "=" * 60)
    print("SUMMARY: total = eliminate + adjust + keep  AND  confidence sum = total")
    print("=" * 60)
    for s in summaries:
        did = s["decision_id"]; asin = s["parent_asin"]
        cur.execute("""SELECT total_count, eliminate_count, adjust_count, keep_count,
            confidence_high_count, confidence_medium_count, confidence_low_count,
            budget_impact, validation_passed, alert_count, alert_msg
            FROM t_advert_agent_modify_suggest_summary WHERE decision_id=%s""", (did,))
        r = cur.fetchone()
        tc = r["total_count"] or 0
        cat_sum = (r["eliminate_count"] or 0) + (r["adjust_count"] or 0) + (r["keep_count"] or 0)
        conf_sum = (r["confidence_high_count"] or 0) + (r["confidence_medium_count"] or 0) + (r["confidence_low_count"] or 0)
        flags = []
        if tc != cat_sum:
            flags.append(f"total≠cat({cat_sum})")
        if tc > 0 and conf_sum != tc:
            flags.append(f"conf≠total({conf_sum})")
        if r.get("alert_count", 0) > 0:
            flags.append(f"alerts:{r['alert_count']}")
        status = "✓" if not flags else "✗ " + " ".join(flags)
        if flags:
            issues.append(f"SUMMARY {asin} [{did[:14]}]: {' '.join(flags)}")
        print(f"  {asin:20s} total={tc:>3d} elim={r['eliminate_count']:>3d} adj={r['adjust_count']:>3d} keep={r['keep_count']:>3d} conf={conf_sum:>3d}  {status}")

    # ── 7. card key field nulls ──
    print("\n" + "=" * 60)
    print("CARD KEY FIELD COVERAGE")
    print("=" * 60)
    cur.execute("SELECT COUNT(1) AS n FROM t_advert_agent_modify_suggest_card")
    total_cards = cur.fetchone()["n"]
    for col in ["campaign_name", "asin", "keyword", "keyword_match_type", "trigger_rule",
                "suggest_category", "confidence_level", "description", "evidence",
                "confirm_status", "execute_status"]:
        cur.execute(f"SELECT COUNT(1) AS n FROM t_advert_agent_modify_suggest_card WHERE {col} IS NULL OR {col} = ''")
        n = cur.fetchone()["n"]
        if n > 0:
            issues.append(f"NULL/EMPTY {col} in cards: {n}/{total_cards}")
        print(f"  {col:25s} null/empty: {n:>4d}/{total_cards}  {'✓' if n == 0 else '✗'}")

    # ── 8. metrics ──
    print("\n" + "=" * 60)
    print("METRICS: type distribution & null check")
    print("=" * 60)
    cur.execute("SELECT metrics_type, COUNT(1) AS n FROM t_advert_agent_data_metrics GROUP BY metrics_type ORDER BY metrics_type")
    for r in cur.fetchall():
        cur.execute(f"SELECT COUNT(1) AS n FROM t_advert_agent_data_metrics WHERE metrics_type=%s AND (avg_daily_sale_num IS NULL OR acos IS NULL OR cvr IS NULL)", (r["metrics_type"],))
        nn = cur.fetchone()["n"]
        print(f"  {r['metrics_type']:15s} {r['n']:>4d} rows  (null metrics: {nn})")

    # ── 9. empty vs populated decisions ──
    print("\n" + "=" * 60)
    print("DATA QUALITY: with / without cards")
    print("=" * 60)
    cur.execute("SELECT s.parent_asin, s.decision_id, s.total_count, COUNT(c.id) AS cards FROM t_advert_agent_modify_suggest_summary s LEFT JOIN t_advert_agent_modify_suggest_card c ON c.decision_id = s.decision_id GROUP BY 1,2,3 HAVING cards = 0")
    empty = cur.fetchall()
    cur.execute("SELECT s.parent_asin, s.decision_id, s.total_count, COUNT(c.id) AS cards FROM t_advert_agent_modify_suggest_summary s LEFT JOIN t_advert_agent_modify_suggest_card c ON c.decision_id = s.decision_id GROUP BY 1,2,3 HAVING cards > 0")
    populated = cur.fetchall()
    print(f"  populated decisions: {len(populated)}")
    for p in populated:
        print(f"    {p['parent_asin']:20s} {p['decision_id'][:14]}  cards={p['cards']}")
    if empty:
        print(f"  empty decisions (stale): {len(empty)}")
        for e in empty:
            print(f"    {e['parent_asin']:20s} {e['decision_id'][:14]}  (no cards — probably from unreachable Doris host)")
            issues.append(f"STALE EMPTY DECISION: {e['parent_asin']} {e['decision_id'][:14]}")
    else:
        print(f"  empty decisions: {len(empty)} ✓")

    # ── verdict ──
    print("\n" + "=" * 60)
    print("VERDICT")
    print("=" * 60)
    if issues:
        print(f"  {len(issues)} ISSUE(S):")
        for i in issues:
            print(f"    - {i}")
    else:
        print("  ALL STRUCTURAL CHECKS PASSED ✓")
    print(f"  {len(ok)} integrity checks passed")
    conn.close()


if __name__ == "__main__":
    main()
