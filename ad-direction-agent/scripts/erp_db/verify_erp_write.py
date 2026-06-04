from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pymysql

from _bootstrap import bootstrap_sys_path
from _erp_conn import ERP_DEFAULT

bootstrap_sys_path()

from app.persistence.erp_writer.mappers import canonicalize_payload  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify ERP dual write result for one JSONL payload.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--shop-id", type=int, default=None)
    parser.add_argument("--parent-seller-sku", default=None)
    parser.add_argument("--site-code", default=None)
    parser.add_argument("--host", default=ERP_DEFAULT["host"])
    parser.add_argument("--port", type=int, default=ERP_DEFAULT["port"])
    parser.add_argument("--user", default=ERP_DEFAULT["user"])
    parser.add_argument("--password", default=ERP_DEFAULT["password"])
    parser.add_argument("--database", default=ERP_DEFAULT["database"])
    return parser.parse_args()


def _read_first_json(path: Path) -> dict:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            return json.loads(line)
    raise ValueError("input jsonl is empty")


def _count(cur, table: str, decision_id: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {table} WHERE decision_id=%s", (decision_id,))
    return int(cur.fetchone()[0])


def main() -> None:
    args = _parse_args()
    payload = _read_first_json(Path(args.input))
    run = canonicalize_payload(
        payload,
        shop_id=args.shop_id,
        parent_seller_sku=args.parent_seller_sku,
        site_code=args.site_code,
    )

    expected = {
        "t_advert_agent_modify_suggest_summary": 1,
        "t_advert_agent_modify_suggest_card": len(run.cards),
        "t_advert_agent_modify_keyword_pending": sum(len(c.keyword_pending) for c in run.cards),
        "t_advert_agent_modify_campaign_pending": sum(len(c.campaign_pending) for c in run.cards),
        "t_advert_agent_modify_placement_pending": sum(len(c.placements) for c in run.cards),
        "t_advert_agent_direction_recommend": 1,
        # Legacy detail has unique(direction_recommend_id, direction_type).
        "t_advert_agent_direction_recommend_detail": len({d.direction_type for d in run.legacy_details}),
        "t_advert_agent_data_metrics": len(run.metrics_rows),
    }
    expected_summary = sum(1 for r in run.metrics_rows if r.get("metrics_type") == "SUMMARY")
    expected_daily = sum(1 for r in run.metrics_rows if r.get("metrics_type") == "DAILY")

    conn = pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        charset="utf8mb4",
    )
    actual = {}
    with conn.cursor() as cur:
        for table in expected:
            actual[table] = _count(cur, table, run.decision_id)
        cur.execute(
            "SELECT metrics_type, COUNT(*) FROM t_advert_agent_data_metrics "
            "WHERE decision_id=%s GROUP BY metrics_type",
            (run.decision_id,),
        )
        metrics_type_counts = {k: int(v) for k, v in cur.fetchall()}
    conn.close()

    checks = {table: {"expected": expected[table], "actual": actual[table], "ok": expected[table] == actual[table]} for table in expected}
    checks["t_advert_agent_data_metrics_summary"] = {
        "expected": expected_summary,
        "actual": metrics_type_counts.get("SUMMARY", 0),
        "ok": expected_summary == metrics_type_counts.get("SUMMARY", 0),
    }
    checks["t_advert_agent_data_metrics_daily"] = {
        "expected": expected_daily,
        "actual": metrics_type_counts.get("DAILY", 0),
        "ok": expected_daily == metrics_type_counts.get("DAILY", 0),
    }
    report = {
        "decision_id": run.decision_id,
        "all_ok": all(v["ok"] for v in checks.values()),
        "metrics_type_counts": metrics_type_counts,
        "checks": checks,
    }
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"verified_decision_id={run.decision_id} all_ok={report['all_ok']}")


if __name__ == "__main__":
    main()

