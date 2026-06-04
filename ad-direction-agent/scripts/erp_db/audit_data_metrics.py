"""Audit t_advert_agent_data_metrics null coverage."""
from __future__ import annotations

import json
from pathlib import Path

import pymysql

from _bootstrap import REPO_ROOT
from _erp_conn import erp_pymysql_conn

COLS = [
    "avg_daily_sale_num",
    "acos",
    "organic_order_rate",
    "tacos",
    "overall_cvr",
    "cvr",
    "ctr",
    "cpc",
    "daily_cost",
]

DECISIONS = [
    ("B0B7", "dec555e98e7cf27ac40ba2f2cd3209b9"),
    ("B0CGH9", "dec5da0a62fe024fa952815cd44be083"),
]


def main() -> None:
    conn = pymysql.connect(**erp_pymysql_conn())
    cur = conn.cursor()

    for label, did in DECISIONS:
        print(f"=== {label} {did} ===")
        cur.execute(
            "SELECT * FROM t_advert_agent_data_metrics WHERE decision_id=%s "
            "ORDER BY metrics_type, day_str",
            (did,),
        )
        rows = cur.fetchall()
        print(f"rows: {len(rows)}")
        for r in rows:
            filled = [c for c in COLS if r.get(c) is not None]
            print(
                f"  {r['metrics_type']:7s} day={r.get('day_str')!s:10s} "
                f"filled={len(filled)}/9 {filled}"
            )
        for mt in ("SUMMARY", "DAILY"):
            cur.execute(
                f"SELECT metrics_type, COUNT(*) n, "
                + ", ".join(f"SUM({c} IS NOT NULL) has_{c}" for c in COLS)
                + " FROM t_advert_agent_data_metrics WHERE decision_id=%s AND metrics_type=%s",
                (did, mt),
            )
            s = cur.fetchone()
            if s and s["n"]:
                print(f"  aggregate {mt} (n={s['n']}):")
                for c in COLS:
                    print(f"    {c}: {s[f'has_{c}']}/{s['n']}")
        print()

    print("=== kb.jsonl source (what Agent can map) ===")
    root = REPO_ROOT / "cursor临时文件"
    for asin in ("B0B7S3PWWB", "B0CGH9QRKK"):
        p = root / f"{asin}_kb.jsonl"
        kb = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
        sc = kb.get("strategy_context") or {}
        dm = kb.get("daily_metrics") or []
        print(f"{asin}:")
        print(f"  strategy_context keys: avg_daily_sales_30d={sc.get('avg_daily_sales_30d')}, "
              f"natural_order_ratio={sc.get('natural_order_ratio')}")
        print(f"  daily_metrics count={len(dm)} sample={dm[0] if dm else None}")

    print("=== global null rate by metrics_type ===")
    cur.execute(
        "SELECT metrics_type, COUNT(*) n, "
        + ", ".join(f"SUM({c} IS NULL) null_{c}" for c in COLS)
        + " FROM t_advert_agent_data_metrics GROUP BY metrics_type"
    )
    for r in cur.fetchall():
        print(r)

    conn.close()


if __name__ == "__main__":
    main()
