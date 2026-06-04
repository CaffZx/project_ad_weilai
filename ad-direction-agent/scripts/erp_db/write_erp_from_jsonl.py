"""Write ERP analysis-suggest tables from Campaign KB JSONL (write_dual)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _bootstrap import bootstrap_sys_path

bootstrap_sys_path()

from _erp_conn import ERP_DEFAULT  # noqa: E402

from app.persistence.erp_writer import ErpDualWriterRepository, canonicalize_payload  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write ERP dual tables from experiment JSONL.")
    parser.add_argument("--input", required=True, help="Input JSONL file path.")
    parser.add_argument("--report", required=True, help="Output report JSON path.")
    parser.add_argument("--canonical-out", default="", help="Optional canonical JSONL output path.")
    parser.add_argument("--shop-id", type=int, default=None)
    parser.add_argument("--parent-seller-sku", default=None)
    parser.add_argument("--site-code", default=None)
    parser.add_argument("--host", default=ERP_DEFAULT["host"])
    parser.add_argument("--port", type=int, default=ERP_DEFAULT["port"])
    parser.add_argument("--user", default=ERP_DEFAULT["user"])
    parser.add_argument("--password", default=ERP_DEFAULT["password"])
    parser.add_argument("--database", default=ERP_DEFAULT["database"])
    return parser.parse_args()


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def main() -> None:
    args = _parse_args()
    input_path = Path(args.input)
    report_path = Path(args.report)
    canonical_out_path = Path(args.canonical_out) if args.canonical_out else None

    rows = _read_jsonl(input_path)
    repo = ErpDualWriterRepository(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
    )
    report_rows: list[dict] = []
    canonical_rows: list[dict] = []
    for payload in rows:
        run = canonicalize_payload(
            payload,
            shop_id=args.shop_id,
            parent_seller_sku=args.parent_seller_sku,
            site_code=args.site_code,
        )
        canonical_rows.append(
            {
                "decision_id": run.decision_id,
                "batch_no": run.batch_no,
                "parent_asin": run.parent_asin,
                "total_campaigns": run.total_campaigns,
                "cards": len(run.cards),
                "legacy_details": len(run.legacy_details),
                "metrics_rows": len(run.metrics_rows),
            }
        )
        rep = repo.write_dual(run)
        report_rows.append(rep.as_dict())

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report_rows, ensure_ascii=False, indent=2), encoding="utf-8")
    if canonical_out_path:
        canonical_out_path.parent.mkdir(parents=True, exist_ok=True)
        with canonical_out_path.open("w", encoding="utf-8") as f:
            for row in canonical_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"written_records={len(rows)} report={report_path}")


if __name__ == "__main__":
    main()
