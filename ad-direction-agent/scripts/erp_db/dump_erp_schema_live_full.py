"""Dump live ERP schema + row counts + sample enum values."""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pymysql

from _bootstrap import REPO_ROOT
from _erp_conn import ERP_DEFAULT, erp_pymysql_conn

LATEST_DECISIONS = {
    "B0B7S3PWWB": "dec555e98e7cf27ac40ba2f2cd3209b9",
    "B0CGH9QRKK": "dec5da0a62fe024fa952815cd44be083",
}


def ser(o):
    if isinstance(o, (Decimal,)):
        return float(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, dict):
        return {k: ser(v) for k, v in o.items()}
    if isinstance(o, list):
        return [ser(x) for x in o]
    return o


def main() -> None:
    conn = pymysql.connect(**erp_pymysql_conn(read_timeout=120))
    cur = conn.cursor()

    cur.execute(
        """
        SELECT TABLE_NAME, TABLE_COMMENT, TABLE_ROWS
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = 'erp_agentadvert'
          AND TABLE_NAME LIKE 't_advert_agent%'
        ORDER BY TABLE_NAME
        """
    )
    tables = cur.fetchall()

    out: dict = {"database": "erp_agentadvert", "tables": {}}

    for t in tables:
        name = t["TABLE_NAME"]
        cur.execute(f"SHOW FULL COLUMNS FROM `{name}`")
        cols = cur.fetchall()
        cur.execute(f"SELECT COUNT(*) AS n FROM `{name}`")
        total = cur.fetchone()["n"]

        col_info = []
        for c in cols:
            col_info.append(
                {
                    "field": c["Field"],
                    "type": c["Type"],
                    "null": c["Null"],
                    "key": c["Key"],
                    "default": c["Default"],
                    "comment": (c.get("Comment") or "").strip(),
                }
            )

        # per-table stats for latest decisions if column exists
        decision_stats = {}
        col_names = {c["Field"] for c in cols}
        if "decision_id" in col_names:
            for asin, did in LATEST_DECISIONS.items():
                cur.execute(f"SELECT COUNT(*) n FROM `{name}` WHERE decision_id=%s", (did,))
                decision_stats[asin] = {"decision_id": did, "rows": cur.fetchone()["n"]}
        elif name == "t_advert_agent_decision":
            for asin, did in LATEST_DECISIONS.items():
                cur.execute(f"SELECT COUNT(*) n FROM `{name}` WHERE id=%s", (did,))
                decision_stats[asin] = {"decision_id": did, "rows": cur.fetchone()["n"]}
        elif "parent_asin" in col_names:
            for asin, did in LATEST_DECISIONS.items():
                cur.execute(
                    f"SELECT COUNT(*) n FROM `{name}` WHERE parent_asin=%s", (asin,)
                )
                decision_stats[asin] = {"rows_for_asin": cur.fetchone()["n"]}

        # sample one row from latest B0B7 decision when possible
        sample = None
        if "decision_id" in col_names:
            did = LATEST_DECISIONS["B0B7S3PWWB"]
            cur.execute(f"SELECT * FROM `{name}` WHERE decision_id=%s LIMIT 1", (did,))
            sample = cur.fetchone()
        elif name == "t_advert_agent_decision":
            cur.execute(
                "SELECT * FROM `%s` WHERE id=%s LIMIT 1" % (name, "%s"),
                (LATEST_DECISIONS["B0B7S3PWWB"],),
            )
            sample = cur.fetchone()
        elif "parent_asin" in col_names:
            cur.execute(
                f"SELECT * FROM `{name}` WHERE parent_asin=%s ORDER BY update_time DESC LIMIT 1",
                ("B0B7S3PWWB",),
            )
            sample = cur.fetchone()

        # distinct values for short varchar enum-like columns (global, capped)
        distincts: dict[str, list] = {}
        for c in cols:
            ctype = (c["Type"] or "").lower()
            field = c["Field"]
            if "varchar" in ctype and int(ctype.split("(")[1].split(")")[0]) <= 64:
                try:
                    cur.execute(
                        f"SELECT DISTINCT `{field}` AS v, COUNT(*) n FROM `{name}` "
                        f"WHERE `{field}` IS NOT NULL AND `{field}` != '' "
                        f"GROUP BY `{field}` ORDER BY n DESC LIMIT 25"
                    )
                    distincts[field] = [ser(r) for r in cur.fetchall()]
                except Exception:
                    pass

        out["tables"][name] = {
            "comment": (t.get("TABLE_COMMENT") or "").strip(),
            "approx_rows": t.get("TABLE_ROWS"),
            "actual_row_count": total,
            "columns": col_info,
            "latest_decision_stats": decision_stats,
            "sample_row_b0b7": ser(sample) if sample else None,
            "distinct_values": distincts,
        }

    (REPO_ROOT / "cursor临时文件" / "erp_schema_live_full.json").write_text(
        json.dumps(ser(out), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("written erp_schema_live_full.json tables=", len(out["tables"]))
    conn.close()


if __name__ == "__main__":
    main()
