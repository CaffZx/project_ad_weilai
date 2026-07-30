"""Apply unified strategy layer preset to ASINs (MySQL state)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJ = Path(__file__).resolve().parents[1]
if str(_PROJ) not in sys.path:
    sys.path.insert(0, str(_PROJ))

from app.persistence.mysql_state_manager import MySQLStateManager

PRESET = {
    "product_level": "重点产品 (P1)",
    "product_stage": None,
    "season_stage": "旺季准备",
}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--asins", required=True, help="Comma-separated parent ASINs")
    p.add_argument("--clear-tactics-cache", action="store_true", default=True)
    args = p.parse_args()
    asins = [a.strip() for a in args.asins.split(",") if a.strip()]
    sm = MySQLStateManager()
    for asin in asins:
        ok = sm.set_long_term_config(asin, PRESET)
        print(f"[{asin}] strategy preset applied={ok} {PRESET}")
        if args.clear_tactics_cache:
            wf = sm.get_workflow_state(asin) or {}
            wf.pop("target_scores", None)
            wf.pop("keyword_analysis", None)
            sm.set_workflow_state(asin, wf)
            print(f"[{asin}] cleared target_scores / keyword_analysis cache")


if __name__ == "__main__":
    main()
