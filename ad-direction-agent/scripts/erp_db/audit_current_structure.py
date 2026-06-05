"""Audit ERP structure for current batch decisions (erp_batch_sequential_status.json)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pymysql

from _bootstrap import REPO_ROOT
from _erp_conn import erp_pymysql_conn

STATUS = REPO_ROOT / "cursor临时文件" / "erp_batch_sequential_status.json"
VALID_DIR = {"PUSH_NATURAL", "EXPAND_KEYWORDS", "OPTIMIZE_ACOS", "BALANCE_MAINTAIN"}

TABLES = [
    ("t_advert_agent_modify_suggest_summary", 1),
    ("t_advert_agent_modify_suggest_card", None),  # per batch cards
    ("t_advert_agent_modify_keyword_pending", None),
    ("t_advert_agent_modify_campaign_pending", None),
    ("t_advert_agent_modify_placement_pending", None),
    ("t_advert_agent_data_metrics", 1),
    ("t_advert_agent_direction_recommend", 1),
    ("t_advert_agent_direction_recommend_detail", 4),
    ("t_advert_agent_purpose_score", 4),
    ("t_advert_agent_ai_suggest", 1),
]


def main() -> int:
    if not STATUS.exists():
        print(f"Missing {STATUS}")
        return 1
    batch = json.loads(STATUS.read_text(encoding="utf-8")).get("results") or []
    issues: list[str] = []

    conn = pymysql.connect(**erp_pymysql_conn())
    cur = conn.cursor()

    print("=" * 72)
    print("ERP STRUCTURE AUDIT — current batch")
    print("=" * 72)

    cur.execute("SELECT COUNT(1) AS n FROM t_advert_agent_decision")
    dec_n = cur.fetchone()["n"]
    print(f"decision rows: {dec_n} (expect {len(batch)})")
    if dec_n != len(batch):
        cur.execute(
            "SELECT parent_asin, id, update_time FROM t_advert_agent_decision ORDER BY parent_asin"
        )
        for r in cur.fetchall():
            print(f"  {r['parent_asin']} | {r['id']} | {r['update_time']}")
        if dec_n > len(batch):
            issues.append(f"extra decision rows: {dec_n} > {len(batch)}")

    for entry in batch:
        asin = entry["asin"]
        did = entry["decision_id"]
        exp_cards = int(entry.get("cards") or 0)
        print(f"\n--- {asin} | {did} | cards={exp_cards} ---")

        cur.execute(
            """
            SELECT shop_id, site_code, product_position, product_stage, season_type,
                   target_acos_suggest, daily_budget_suggest, advert_direction_types
            FROM t_advert_agent_decision WHERE id=%s
            """,
            (did,),
        )
        dec = cur.fetchone()
        if not dec:
            issues.append(f"{asin}: decision missing")
            continue
        print(
            f"  site={dec['site_code']} pos={dec['product_position']} "
            f"stage={dec['product_stage']} target_acos={dec['target_acos_suggest']} "
            f"budget={dec['daily_budget_suggest']}"
        )

        for table, expected in TABLES:
            cur.execute(f"SELECT COUNT(1) AS n FROM `{table}` WHERE decision_id=%s", (did,))
            n = cur.fetchone()["n"]
            exp = expected if expected is not None else exp_cards
            ok = n == exp or (
                table.endswith("_pending") and n > 0 and n <= exp_cards * 3
            )
            if table == "t_advert_agent_core_keyword_tracking":
                ok = n >= 0
            if expected is not None and n != expected:
                issues.append(f"{asin} {table}: {n} != {expected}")
            elif table == "t_advert_agent_modify_suggest_card" and n != exp_cards:
                issues.append(f"{asin} cards: {n} != {exp_cards}")
            print(f"  {table:45s} {n:4d}  {'OK' if ok else 'CHECK'}")

        cur.execute(
            "SELECT COUNT(1) AS n FROM t_advert_agent_core_keyword_tracking WHERE decision_id=%s",
            (did,),
        )
        kw_n = cur.fetchone()["n"]
        print(f"  {'t_advert_agent_core_keyword_tracking':45s} {kw_n:4d}")

        cur.execute(
            """
            SELECT direction_type, suggest_score, recommend_tag, sort_order, content_json
            FROM t_advert_agent_direction_recommend_detail
            WHERE decision_id=%s ORDER BY sort_order
            """,
            (did,),
        )
        for r in cur.fetchall():
            if r["direction_type"] not in VALID_DIR:
                issues.append(f"{asin} invalid direction_type {r['direction_type']}")
            raw_content = (r["content_json"] or "").strip()
            preview = ""
            if not raw_content.startswith("["):
                issues.append(f"{asin} {r['direction_type']} content_json not JSON array")
            else:
                try:
                    lines = json.loads(raw_content)
                except json.JSONDecodeError:
                    issues.append(f"{asin} {r['direction_type']} content_json invalid JSON")
                    lines = []
                if not isinstance(lines, list):
                    issues.append(f"{asin} {r['direction_type']} content_json must be array")
                elif lines and not all(isinstance(x, str) for x in lines):
                    issues.append(f"{asin} {r['direction_type']} content_json must be string array")
                preview = (lines[0] if lines else "")[:24]
            preview = preview + ("…" if len(preview) >= 24 else "")
            print(
                f"  Tab4 {r['direction_type']:16s} {r['suggest_score']:3d} "
                f"{r['recommend_tag']:16s} reason={preview}"
            )

        types = json.loads(dec.get("advert_direction_types") or "[]")
        bad = [t for t in types if t not in VALID_DIR]
        if bad:
            issues.append(f"{asin} legacy advert_direction_types: {bad}")

        cur.execute(
            "SELECT total_count FROM t_advert_agent_modify_suggest_summary WHERE decision_id=%s",
            (did,),
        )
        summ = cur.fetchone()
        if summ:
            print(f"  summary.total_count={summ['total_count']} (cards={exp_cards})")

    print("\n" + "=" * 72)
    print("GLOBAL")
    cur.execute(
        "SELECT decision_id, campaign_id, COUNT(1) AS n "
        "FROM t_advert_agent_modify_suggest_card GROUP BY 1,2 HAVING n > 1"
    )
    dup = len(cur.fetchall())
    print(f"  card UK duplicates: {dup}")
    if dup:
        issues.append("card UK duplicates")

    for p in ("keyword_pending", "campaign_pending", "placement_pending"):
        tbl = f"t_advert_agent_modify_{p}"
        cur.execute(
            f"SELECT COUNT(1) AS n FROM {tbl} p "
            "LEFT JOIN t_advert_agent_modify_suggest_card c ON p.suggest_card_id=c.id "
            "WHERE c.id IS NULL"
        )
        o = cur.fetchone()["n"]
        print(f"  orphan {p}: {o}")
        if o:
            issues.append(f"orphan {p}: {o}")

    cur.execute("SELECT COUNT(1) AS n FROM t_advert_agent_direction_recommend_detail")
    det = cur.fetchone()["n"]
    exp_det = len(batch) * 4
    print(f"  direction_detail total: {det} (expect {exp_det})")
    if det != exp_det:
        issues.append(f"direction_detail total {det} != {exp_det}")

    cur.execute("SELECT COUNT(1) AS n FROM t_advert_agent_modify_suggest_card")
    cards = cur.fetchone()["n"]
    exp_cards_sum = sum(int(e.get("cards") or 0) for e in batch)
    print(f"  suggest_card total: {cards} (expect {exp_cards_sum})")
    if cards != exp_cards_sum:
        issues.append(f"card total {cards} != {exp_cards_sum}")

    conn.close()
    print("=" * 72)
    if issues:
        print(f"VERDICT: {len(issues)} ISSUE(S)")
        for i in issues:
            print(f"  - {i}")
        return 1
    print("VERDICT: ALL STRUCTURE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
