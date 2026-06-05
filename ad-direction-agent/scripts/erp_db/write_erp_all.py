"""Write Campaign + Wizard JSONL outputs to ERP database (write_full)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _bootstrap import bootstrap_sys_path

bootstrap_sys_path()

from app.persistence.erp_writer.auto_push import erp_connection_kwargs, push_full_to_erp  # noqa: E402
from app.persistence.erp_writer import resolve_listing_context  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Full ERP write from kb + wizard JSONL")
    p.add_argument("--asin", required=True)
    p.add_argument("--kb", required=True, help="Campaign kb.jsonl path")
    p.add_argument("--wizard", default="", help="Wizard jsonl path")
    p.add_argument("--report", required=True)
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--user", default=None)
    p.add_argument("--password", default=None)
    p.add_argument("--database", default=None)
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

    conn = erp_connection_kwargs()
    if args.host:
        conn["host"] = args.host
    if args.port is not None:
        conn["port"] = args.port
    if args.user:
        conn["user"] = args.user
    if args.password:
        conn["password"] = args.password
    if args.database:
        conn["database"] = args.database

    report = push_full_to_erp(kb_payload, wizard_payload, conn_kwargs=conn)

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
