"""Truncate all t_advert_agent_* tables in ERP database (destructive)."""
from __future__ import annotations

import argparse
import sys

import pymysql

from _erp_conn import ERP_DEFAULT

TRUNCATE_ORDER = [
    "t_advert_agent_modify_keyword_pending",
    "t_advert_agent_modify_campaign_pending",
    "t_advert_agent_modify_placement_pending",
    "t_advert_agent_modify_suggest_card",
    "t_advert_agent_modify_suggest_summary",
    "t_advert_agent_data_metrics",
    "t_advert_agent_purpose_score",
    "t_advert_agent_core_keyword_tracking",
    "t_advert_agent_ai_suggest",
    "t_advert_agent_direction_recommend_detail",
    "t_advert_agent_direction_recommend",
    "t_advert_agent_decision_config",
    "t_advert_agent_modify_advert_record",
    "t_advert_agent_modify_keyword_record",
    "t_advert_agent_modify_campaign_record",
    "t_advert_agent_modify_placement_record",
    "t_advert_agent_modify_portfolio_record",
    "t_advert_agent_decision",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="TRUNCATE all t_advert_agent_* ERP tables")
    parser.add_argument("--host", default=ERP_DEFAULT["host"])
    parser.add_argument("--port", type=int, default=ERP_DEFAULT["port"])
    parser.add_argument("--user", default=ERP_DEFAULT["user"])
    parser.add_argument("--password", default=ERP_DEFAULT["password"])
    parser.add_argument("--database", default=ERP_DEFAULT["database"])
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required flag; refuses to run without explicit confirmation",
    )
    args = parser.parse_args()
    if not args.yes:
        print("Refusing to truncate without --yes", file=sys.stderr)
        return 2

    conn = pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        charset="utf8mb4",
        connect_timeout=15,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            for table in TRUNCATE_ORDER:
                cur.execute(f"TRUNCATE TABLE `{table}`")
                print(f"truncated {table}")
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        conn.commit()
    finally:
        conn.close()

    print(f"done: {len(TRUNCATE_ORDER)} tables truncated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
