"""Write Campaign + Wizard JSONL outputs to ERP database (write_full)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _bootstrap import bootstrap_sys_path

bootstrap_sys_path()

from _erp_conn import ERP_DEFAULT  # noqa: E402

from app.persistence.erp_writer import (  # noqa: E402
    ErpDualWriterRepository,
    canonicalize_payload,
    resolve_listing_context,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Full ERP write from kb + wizard JSONL")
    p.add_argument("--asin", required=True)
    p.add_argument("--kb", required=True, help="Campaign kb.jsonl path")
    p.add_argument("--wizard", default="", help="Wizard jsonl path")
    p.add_argument("--report", required=True)
    p.add_argument("--host", default=ERP_DEFAULT["host"])
    p.add_argument("--port", type=int, default=ERP_DEFAULT["port"])
    p.add_argument("--user", default=ERP_DEFAULT["user"])
    p.add_argument("--password", default=ERP_DEFAULT["password"])
    p.add_argument("--database", default=ERP_DEFAULT["database"])
    return p.parse_args()


def _read_jsonl(path: Path) -> dict:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            return json.loads(line)
    raise ValueError(f"empty jsonl: {path}")


def main() -> None:
    args = _parse_args()
    listing = resolve_listing_context(args.asin)
    kb_payload = _read_jsonl(Path(args.kb))
    wizard_payload = _read_jsonl(Path(args.wizard)) if args.wizard else {}

    decision_meta = wizard_payload.get("decision_meta") or {}
    if wizard_payload.get("long_term_config"):
        lt = wizard_payload["long_term_config"]
        decision_meta.setdefault("product_position", lt.get("product_level"))
        decision_meta.setdefault("product_stage", lt.get("product_stage"))
        decision_meta.setdefault("season_type", lt.get("season_stage"))
        decision_meta.setdefault("ad_purposes", lt.get("ad_purposes"))
        decision_meta.setdefault("target_keyword_types", lt.get("target_keyword_strategy"))

    kb_payload["decision_meta"] = decision_meta
    kb_payload["shop_id"] = listing.shop_id

    run = canonicalize_payload(
        kb_payload,
        shop_id=listing.shop_id,
        parent_seller_sku=listing.parent_seller_sku,
        site_code=listing.site_code,
    )
    decision_meta["site_code"] = listing.site_code

    repo = ErpDualWriterRepository(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
    )
    report = repo.write_full(
        run,
        wizard_payload=wizard_payload or None,
        decision_meta=decision_meta,
    )

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "asin": args.asin,
                "listing": {
                    "shop_id": listing.shop_id,
                    "parent_seller_sku": listing.parent_seller_sku,
                    "site_code": listing.site_code,
                },
                "write": report.as_dict(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"written decision_id={report.decision_id} report={report_path}")


if __name__ == "__main__":
    main()
